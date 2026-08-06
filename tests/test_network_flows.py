import importlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from unittest.mock import patch


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        payload=None,
        content=b"data",
        text="",
        chunks=None,
        stream_error=None,
    ):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self.text = text
        self.chunks = list(chunks) if chunks is not None else [content]
        self.stream_error = stream_error
        self.content_accessed = False
        self.closed = False
        self.iter_content_chunk_size = None

    @property
    def content(self):
        self.content_accessed = True
        return self._content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=None):
        self.iter_content_chunk_size = chunk_size
        yield from self.chunks
        if self.stream_error is not None:
            raise self.stream_error

    def close(self):
        self.closed = True


class FakeRequests:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def load_module(module_name, fake_requests):
    curl_cffi = types.ModuleType("curl_cffi")
    curl_cffi.requests = fake_requests
    sys.modules["curl_cffi"] = curl_cffi
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def profile_payload(shortcode=None, *, is_video=False, media_url="https://cdn.example/media"):
    edges = []
    if shortcode is not None:
        node = {"shortcode": shortcode, "is_video": is_video}
        node["video_url" if is_video else "display_url"] = media_url
        edges.append({"node": node})
    return {"data": {"user": {"edge_owner_to_timeline_media": {"edges": edges}}}}


class MediaDownloaderTests(unittest.TestCase):
    def test_rejects_non_instagram_host_before_network(self):
        fake = FakeRequests()
        module = load_module("core.mediaDownloader", fake)

        result = module.download_media("https://evil.example/account/p/ABC123")

        self.assertFalse(result)
        self.assertEqual(fake.calls, [])

    def test_rejects_windows_path_traversal_username_before_network(self):
        fake = FakeRequests()
        module = load_module("core.mediaDownloader", fake)

        result = module.download_media(
            r"https://www.instagram.com/..\..\escape/p/ABC123"
        )

        self.assertFalse(result)
        self.assertEqual(fake.calls, [])

    def test_failed_second_lookup_does_not_reuse_previous_media(self):
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "FIRST", media_url="https://cdn.example/first.png"
                    )
                ),
                FakeResponse(content=b"first"),
                FakeResponse(payload=profile_payload()),
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module.os.path, "abspath", return_value=tmp),
        ):
            first_result = module.download_media(
                "https://www.instagram.com/alice/p/FIRST"
            )
            second_result = module.download_media(
                "https://www.instagram.com/alice/p/SECOND"
            )

        self.assertTrue(first_result)
        self.assertFalse(second_result)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[2][1]["params"], {"username": "alice"})

    def test_filename_uses_actual_media_type_and_full_shortcode(self):
        shortcode = "VID1234567-EXTRA"
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        shortcode,
                        is_video=True,
                        media_url="https://cdn.example/video.mp4",
                    )
                ),
                FakeResponse(content=b"video"),
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module.os.path, "abspath", return_value=tmp),
        ):
            result = module.download_media(
                f"https://www.instagram.com/alice/p/{shortcode}"
            )
            path = Path(tmp, "InstaDownloads", f"alice-reel-{shortcode}.mp4")
            saved = path.read_bytes()

        self.assertTrue(result)
        self.assertEqual(saved, b"video")

    def test_network_requests_have_explicit_timeout(self):
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "POST1", media_url="https://cdn.example/image.png"
                    )
                ),
                FakeResponse(content=b"image"),
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module.os.path, "abspath", return_value=tmp),
        ):
            result = module.download_media(
                "https://www.instagram.com/alice/p/POST1"
            )

        self.assertTrue(result)
        self.assertEqual([call[1]["timeout"] for call in fake.calls], [30, 30])

    def test_network_requests_use_safe_redirects_and_stream_media(self):
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "POST1", media_url="https://cdn.example/image.png"
                    )
                ),
                FakeResponse(content=b"image"),
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module.os.path, "abspath", return_value=tmp),
        ):
            result = module.download_media(
                "https://www.instagram.com/alice/p/POST1"
            )

        self.assertTrue(result)
        self.assertEqual(
            [call[1]["allow_redirects"] for call in fake.calls],
            ["safe", "safe"],
        )
        self.assertTrue(fake.calls[1][1]["stream"])

    def test_download_streams_without_buffering_full_response(self):
        download_response = FakeResponse(
            content=b"buffered-copy",
            chunks=[b"ima", b"ge"],
        )
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "POST1", media_url="https://cdn.example/image.png"
                    )
                ),
                download_response,
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module.os.path, "abspath", return_value=tmp),
        ):
            result = module.download_media(
                "https://www.instagram.com/alice/p/POST1"
            )
            saved = Path(tmp, "InstaDownloads", "alice-post-POST1.png").read_bytes()

        self.assertTrue(result)
        self.assertEqual(saved, b"image")
        self.assertFalse(download_response.content_accessed)
        self.assertIsNone(download_response.iter_content_chunk_size)
        self.assertTrue(download_response.closed)

    @unittest.skipIf(os.name == "nt", "POSIX file modes do not apply on Windows")
    def test_downloaded_media_uses_owner_only_permissions(self):
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "POST1", media_url="https://cdn.example/image.png"
                    )
                ),
                FakeResponse(content=b"image"),
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with tempfile.TemporaryDirectory() as tmp:
            previous_umask = os.umask(0o022)
            try:
                with patch.object(module.os.path, "abspath", return_value=tmp):
                    result = module.download_media(
                        "https://www.instagram.com/alice/p/POST1"
                    )
            finally:
                os.umask(previous_umask)

            target = Path(tmp, "InstaDownloads", "alice-post-POST1.png")
            mode = stat.S_IMODE(target.stat().st_mode)

        self.assertTrue(result)
        self.assertEqual(mode, 0o600)

    def test_stream_failure_preserves_existing_file_and_removes_partial(self):
        download_response = FakeResponse(
            content=b"replacement",
            chunks=[b"partial"],
            stream_error=RuntimeError("stream failed"),
        )
        fake = FakeRequests(
            [
                FakeResponse(
                    payload=profile_payload(
                        "POST1", media_url="https://cdn.example/image.png"
                    )
                ),
                download_response,
            ]
        )
        module = load_module("core.mediaDownloader", fake)

        with tempfile.TemporaryDirectory() as tmp:
            download_dir = Path(tmp, "InstaDownloads")
            download_dir.mkdir()
            target = download_dir / "alice-post-POST1.png"
            target.write_bytes(b"original")

            with patch.object(module.os.path, "abspath", return_value=tmp):
                result = module.download_media(
                    "https://www.instagram.com/alice/p/POST1"
                )

            remaining_files = sorted(path.name for path in download_dir.iterdir())
            saved = target.read_bytes()

        self.assertFalse(result)
        self.assertEqual(saved, b"original")
        self.assertEqual(remaining_files, [target.name])
        self.assertTrue(download_response.closed)

    def test_network_failure_is_reported_without_traceback(self):
        fake = FakeRequests([RuntimeError("network down")])
        module = load_module("core.mediaDownloader", fake)

        result = module.download_media(
            "https://www.instagram.com/alice/p/POST1"
        )

        self.assertFalse(result)

    def test_malformed_profile_edges_are_rejected_without_traceback(self):
        payload = {
            "data": {
                "user": {
                    "edge_owner_to_timeline_media": {"edges": {"not": "a list"}}
                }
            }
        }
        fake = FakeRequests([FakeResponse(payload=payload)])
        module = load_module("core.mediaDownloader", fake)

        result = module.download_media(
            "https://www.instagram.com/alice/p/POST1"
        )

        self.assertFalse(result)


class AccountDataFetcherTests(unittest.TestCase):
    def test_profile_request_uses_params_timeout_and_returns_success(self):
        payload = {
            "data": {
                "user": {
                    "is_private": False,
                    "edge_owner_to_timeline_media": {"edges": []},
                }
            }
        }
        fake = FakeRequests([FakeResponse(payload=payload)])
        module = load_module("core.accountDataFetcher", fake)

        result = module.fetch_data("alice")

        self.assertTrue(result)
        url, kwargs = fake.calls[0]
        self.assertEqual(url, module.INSTAGRAM_PROFILE_URL)
        self.assertEqual(kwargs["params"], {"username": "alice"})
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["allow_redirects"], "safe")

    def test_profile_network_failure_returns_false(self):
        fake = FakeRequests([RuntimeError("network down")])
        module = load_module("core.accountDataFetcher", fake)

        result = module.fetch_data("alice")

        self.assertFalse(result)


class MainTests(unittest.TestCase):
    def load_main(self):
        fake = FakeRequests()
        curl_cffi = types.ModuleType("curl_cffi")
        curl_cffi.requests = fake
        sys.modules["curl_cffi"] = curl_cffi
        for module_name in (
            "main",
            "core.accountDataFetcher",
            "core.mediaDownloader",
        ):
            sys.modules.pop(module_name, None)
        return importlib.import_module("main")

    def test_download_failure_returns_nonzero_status(self):
        module = self.load_main()
        args = Namespace(name=None, dload="invalid", debug=False)

        with (
            patch.object(module, "getArguments", return_value=args),
            patch.object(module, "printBanner"),
            patch.object(module, "download_media", return_value=False),
        ):
            self.assertEqual(module.main(), 1)

    def test_success_returns_zero_status(self):
        module = self.load_main()
        args = Namespace(name="alice", dload=None, debug=False)

        with (
            patch.object(module, "getArguments", return_value=args),
            patch.object(module, "printBanner"),
            patch.object(module, "fetch_data", return_value=True),
        ):
            self.assertEqual(module.main(), 0)


if __name__ == "__main__":
    unittest.main()
