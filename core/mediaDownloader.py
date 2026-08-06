"""
Copyright (c) 2025 obitouka
See the file 'LICENSE' for copying permission
"""

from datetime import datetime
import os
import re
import tempfile
from urllib.parse import urlparse

from curl_cffi import requests
from utils.colorPrinter import *


INSTAGRAM_PROFILE_URL = "https://www.instagram.com/api/v1/users/web_profile_info/"
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
REQUEST_TIMEOUT_SECONDS = 30
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]+$")
SHORTCODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def get_time():
    return datetime.now().strftime("%H:%M:%S")


def parse_post_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if (
        parsed.scheme.lower() != "https"
        or host not in INSTAGRAM_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 3:
        return None

    username, post_type, shortcode = parts
    if post_type not in ("p", "reel"):
        return None
    if username in (".", "..") or not USERNAME_PATTERN.fullmatch(username):
        return None
    if not SHORTCODE_PATTERN.fullmatch(shortcode):
        return None

    return username, shortcode


def print_invalid_url():
    colorPrint(
        CYAN, f"[{get_time()}] \t",
        RED, "[ERROR] \t",
        RED, "Invalid URL format"
    )
    colorPrint(
        CYAN, f"[{get_time()}] \t",
        YELLOW, "[EXAMPLE] \t",
        LIGHT_BLUE_EX,
        "https://www.instagram.com/keyloggerluvr/p/V2tgdUTWI6kLka3N/\n"
        "                                or\n"
        "https://www.instagram.com/keyloggerluvr/reel/V2tgdUTWI6kLka3N/"
    )


def print_request_error(response, debug, fallback):
    if debug:
        try:
            details = response.json()
        except Exception:
            try:
                details = response.text
            except Exception:
                details = "Response details unavailable"
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, f"[{response.status_code}] \t\t\b",
            YELLOW, "[DEBUG] \t\n\n",
            GREEN, f"{details}"
        )
    elif response.status_code == 429:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[429] \t\t\b",
            YELLOW, "[WARNING] \t",
            RED, "Instagram added rate limit to your IP. Try again later"
        )
    else:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, f"[{response.status_code}] \t\t\b",
            YELLOW, "[WARNING] \t",
            RED, fallback
        )


def fetch_media(url, debug=False):
    parsed_url = parse_post_url(url)
    if parsed_url is None:
        print_invalid_url()
        return None

    username, shortcode = parsed_url
    colorPrint(
        CYAN, f"[{get_time()}] \t",
        GREEN, "[INFO] \t\t",
        LIGHT_YELLOW_EX, "Fetching..."
    )

    try:
        response = requests.get(
            INSTAGRAM_PROFILE_URL,
            params={"username": username},
            headers={"X-IG-App-ID": "936619743392459"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects="safe",
        )
    except Exception as error:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[ERROR] \t",
            RED, str(error) if debug else "Failed to connect to Instagram"
        )
        return None

    if response.status_code != 200:
        print_request_error(response, debug, "Failed to fetch media data")
        return None

    try:
        edges = response.json()["data"]["user"]["edge_owner_to_timeline_media"]["edges"]
        if not isinstance(edges, list):
            raise TypeError("media edges must be a list")
    except (KeyError, TypeError, ValueError) as error:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[ERROR] \t",
            RED, str(error) if debug else "Invalid media data returned by Instagram"
        )
        return None

    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("node"), dict):
            continue
        node = edge["node"]
        if node.get("shortcode") != shortcode:
            continue

        is_video = bool(node.get("is_video"))
        media_url = node.get("video_url" if is_video else "display_url")
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            colorPrint(
                CYAN, f"[{get_time()}] \t",
                RED, "[ERROR] \t",
                RED, "Invalid media URL returned by Instagram"
            )
            return None

        media_type = "reel" if is_video else "post"
        extension = ".mp4" if is_video else ".png"
        file_name = f"{username}-{media_type}-{shortcode}{extension}"
        return media_url, file_name

    colorPrint(
        CYAN, f"[{get_time()}] \t",
        RED, "[ERROR] \t",
        RED, "Post not found in the fetched profile data"
    )
    return None


def download_media(post_url, debug=False):
    media = fetch_media(post_url, debug)
    if media is None:
        return False

    media_url, file_name = media
    colorPrint(
        CYAN, f"[{get_time()}] \t",
        GREEN, "[INFO] \t\t",
        LIGHT_YELLOW_EX, "Downloading..."
    )

    try:
        response = requests.get(
            media_url,
            headers={"X-IG-App-ID": "936619743392459"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects="safe",
            stream=True,
        )
    except Exception as error:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[ERROR] \t",
            RED, str(error) if debug else "Failed to download media"
        )
        return False

    project_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    download_path = os.path.join(project_path, "InstaDownloads")
    file_path = os.path.join(download_path, file_name)
    temporary_path = None

    try:
        if response.status_code != 200:
            print_request_error(response, debug, "Failed to download media")
            return False

        os.makedirs(download_path, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=download_path,
            prefix=f".{file_name}.",
            suffix=".part",
            delete=False,
        ) as file:
            temporary_path = file.name
            for chunk in response.iter_content():
                if chunk:
                    file.write(chunk)

        os.replace(temporary_path, file_path)
        temporary_path = None
    except OSError as error:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[ERROR] \t",
            RED, str(error) if debug else "Failed to save media"
        )
        return False
    except Exception as error:
        colorPrint(
            CYAN, f"[{get_time()}] \t",
            RED, "[ERROR] \t",
            RED, str(error) if debug else "Failed to download media"
        )
        return False
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            response.close()
        except Exception:
            pass

    colorPrint(
        CYAN, f"[{get_time()}] \t",
        GREEN, "[SUCCESS] \t",
        LIGHT_YELLOW_EX, "Downloaded ",
        LIGHT_BLUE_EX, ITALIC, f"{file_name} ", ITALIC_OFF,
        LIGHT_YELLOW_EX, f"at {ITALIC}'InstaDownloads'{ITALIC_OFF} folder"
    )
    return True
