import os
import sys
import logging
import urllib.request
import json
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Public, stable test URLs across supported platforms (metadata only check)
CANARY_TARGETS = {
    "YouTube": "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # Me at the zoo (first YouTube video)
    "TikTok": "https://www.tiktok.com/@pokemonlife22/video/7059698374567611694",  # Stable public video from yt-dlp test suite
    "SoundCloud": "https://soundcloud.com/forss/flickermood",  # Track 293 (Forss - Flickermood)
}

BOT_DETECTION_PATTERNS = (
    "sign in to confirm you’re not a bot",
    "sign in to confirm you're not a bot",
    "use --cookies-from-browser or --cookies",
    "your ip address is blocked",
    "please sign in",
    "http error 429",
    "too many requests",
    "bot verification",
)


def is_anti_bot_error(error_msg: str) -> bool:
    lowered = error_msg.lower()
    return any(pattern in lowered for pattern in BOT_DETECTION_PATTERNS)


def build_canary_options(platform: str) -> dict:
    options = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "discard_in_playlist",
        "socket_timeout": 15,
    }

    proxy = os.getenv("YTDLP_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if proxy and proxy.strip():
        options["proxy"] = proxy.strip()

    if platform == "YouTube":
        cookies_file = os.getenv("YTDLP_YOUTUBE_COOKIES_FILE", "cookies/youtube.txt")
        if os.path.isfile(cookies_file):
            options["cookiefile"] = cookies_file

        player_clients = os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENT")
        if player_clients and player_clients.strip():
            client_list = [c.strip() for c in player_clients.split(",") if c.strip()]
        else:
            client_list = ["android", "mweb", "web"]

        options["extractor_args"] = {
            "youtube": {
                "player_client": client_list,
            }
        }
        options["js_runtimes"] = {"deno": {}, "node": {}}

    return options


def check_tikwm_api(url: str) -> bool:
    """Check TikWM API (primary provider used by Downloader-Bot for TikTok)."""
    try:
        req_url = f"https://tikwm.com/api/?url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(
            req_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("code") == 0
    except Exception as exc:
        logging.debug("TikWM check exception: %s", exc)
        return False


def run_yt_dlp_canary(name: str, url: str) -> tuple[str, str]:
    """Returns (status, detail_message) where status is 'PASS', 'BLOCKED', or 'FAIL'."""
    ydl_opts = build_canary_options(name)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get("title") or info.get("id") or "OK"
                logging.info("Canary PASS [%s]: %s (title: %s)", name, url, title[:50])
                return "PASS", title[:50]
            else:
                logging.error("Canary FAIL [%s]: No info returned for %s", name, url)
                return "FAIL", "No info returned"
    except Exception as exc:
        exc_str = str(exc)

        # For TikTok: if yt-dlp failed, check TikWM (the bot's primary engine)
        if name == "TikTok" and check_tikwm_api(url):
            logging.info("Canary PASS [%s via TikWM API]: %s (yt-dlp fallback note: %s)", name, url, exc_str[:60])
            return "PASS", "Operational via TikWM (bot primary engine)"

        if is_anti_bot_error(exc_str):
            logging.warning(
                "Canary BLOCKED [%s]: Anti-bot / IP challenge on runner IP for %s: %s",
                name,
                url,
                exc_str,
            )
            return "BLOCKED", f"Runner IP blocked by anti-bot verification ({exc_str[:80]}...)"

        logging.error("Canary FAIL [%s]: %s - error: %s", name, url, exc)
        return "FAIL", exc_str


def write_github_summary(results: dict[str, tuple[str, str]]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    status_badges = {
        "PASS": "✅ PASS",
        "BLOCKED": "⚠️ BLOCKED (IP challenge)",
        "FAIL": "❌ FAIL",
    }

    lines = [
        "### 🐤 Platform Canary Test Results\n",
        "| Platform | URL | Status | Details |",
        "| --- | --- | --- | --- |",
    ]
    for name, (status, detail) in results.items():
        badge = status_badges.get(status, status)
        url = CANARY_TARGETS.get(name, "")
        clean_detail = detail.replace("|", "&#124;").replace("\n", " ")
        lines.append(f"| **{name}** | [{name} test URL]({url}) | {badge} | {clean_detail} |")

    lines.append("")
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as exc:
        logging.warning("Failed to write to GITHUB_STEP_SUMMARY: %s", exc)


def main():
    print("Starting platform canary metadata checks...")
    results: dict[str, tuple[str, str]] = {}
    failed_platforms = []
    blocked_platforms = []

    for name, url in CANARY_TARGETS.items():
        status, detail = run_yt_dlp_canary(name, url)
        results[name] = (status, detail)
        if status == "FAIL":
            failed_platforms.append(name)
        elif status == "BLOCKED":
            blocked_platforms.append(name)

    print("\n--- Canary Summary ---")
    for name, (status, detail) in results.items():
        if status == "PASS":
            icon = "✅ PASS"
        elif status == "BLOCKED":
            icon = "⚠️ BLOCKED (Runner IP challenge)"
        else:
            icon = "❌ FAIL"
        print(f"  {name}: {icon} - {detail}")

    write_github_summary(results)

    strict_mode = os.getenv("CANARY_STRICT", "0").lower() in ("1", "true")

    if failed_platforms:
        print(f"\n❌ Canary check failed for platforms: {', '.join(failed_platforms)}")
        sys.exit(1)
    elif blocked_platforms and strict_mode:
        print(f"\n⚠️ Canary check blocked (strict mode enabled): {', '.join(blocked_platforms)}")
        sys.exit(1)
    elif blocked_platforms:
        print(
            f"\n⚠️ Canary note: {', '.join(blocked_platforms)} encountered anti-bot / datacenter IP restrictions. "
            "To test with authentication on GitHub Actions, configure secrets.YOUTUBE_COOKIES or a proxy."
        )
        sys.exit(0)
    else:
        print("\n✅ All canary checks passed successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
