"""
Copyright (c) 2025 obitouka
See the file 'LICENSE' for copying permission
"""

from core.accountDataFetcher import fetch_data
from core.mediaDownloader import download_media
from lib.banner import printBanner
from utils.parser import getArguments


def main():
    args = getArguments()

    if args.name:
        printBanner()
        return 0 if fetch_data(args.name, debug=args.debug) else 1
    if args.dload:
        printBanner()
        return 0 if download_media(args.dload, debug=args.debug) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
