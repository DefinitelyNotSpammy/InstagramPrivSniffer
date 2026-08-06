import importlib
import sys
import types
import unittest
from argparse import Namespace
from unittest.mock import mock_open, patch


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"data", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


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

        with patch("builtins.open", mock_open()):
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
        opened = mock_open()

        with patch("builtins.open", opened):
            result = module.download_media(
                f"https://www.instagram.com/alice/p/{shortcode}"
            )

        self.assertTrue(result)
        path = opened.call_args.args[0]
        self.assertTrue(path.endswith(f"alice-reel-{shortcode}.mp4"), path)

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

        with patch("builtins.open", mock_open()):
            result = module.download_media(
                "https://www.instagram.com/alice/p/POST1"
            )

        self.assertTrue(result)
        self.assertEqual([call[1]["timeout"] for call in fake.calls], [30, 30])

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
