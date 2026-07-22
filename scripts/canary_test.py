import sys
import logging
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Public, stable test URLs across supported platforms (metadata only check)
CANARY_TARGETS = {
    "YouTube": "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # Me at the zoo
    "TikTok": "https://www.tiktok.com/@tiktok/video/7016547803243007238",
    "SoundCloud": "https://soundcloud.com/forss/vlick",
}


def run_yt_dlp_canary(name: str, url: str) -> bool:
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'discard_in_playlist',
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get("title") or info.get("id") or "OK"
                logging.info("Canary PASS [%s]: %s (title: %s)", name, url, title[:40])
                return True
            else:
                logging.error("Canary FAIL [%s]: No info returned for %s", name, url)
                return False
    except Exception as exc:
        logging.error("Canary FAIL [%s]: %s - error: %s", name, url, exc)
        return False


def main():
    print("Starting platform canary metadata checks...")
    results = {}
    failed_platforms = []

    for name, url in CANARY_TARGETS.items():
        ok = run_yt_dlp_canary(name, url)
        results[name] = ok
        if not ok:
            failed_platforms.append(name)

    print("\n--- Canary Summary ---")
    for name, ok in results.items():
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {name}: {status}")

    if failed_platforms:
        print(f"\nCanary check failed for platforms: {', '.join(failed_platforms)}")
        sys.exit(1)
    else:
        print("\nAll canary checks passed successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
