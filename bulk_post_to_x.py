"""
Fetches a video from Google Drive (folder: UPLOAD_FOLDER_ID),
posts it to X with a caption from table.csv, then moves the file
to PROCESSED_FOLDER_ID to avoid re-posting.

Runs in a loop, posting one video every INTERVAL_MINUTES (default: 30).
The GitHub Actions workflow keeps it alive up to the job timeout.

Required secrets / env vars:
    GOOGLE_CREDENTIALS_JSON   - full JSON with client_id, client_secret,
                                refresh_token, token_uri (see README)
    UPLOAD_FOLDER_ID          - Drive folder ID to pull videos from
    PROCESSED_FOLDER_ID       - Drive folder ID to move processed videos to
    X_STORAGE_STATE_JSON      - Playwright saved session for X
    POSTS_CSV_PATH            - path to caption CSV (default: table.csv)
    CAPTION_SOURCE            - "csv" or "custom" (default: csv)
    CUSTOM_CAPTION            - used when CAPTION_SOURCE=custom
    SHUFFLE_ORDER             - "true" to pick a random video (default: false)
    INTERVAL_MINUTES          - minutes between posts (default: 30)
    MAX_POSTS                 - max posts per run, 0 = unlimited (default: 0)
"""

import csv
import json
import os
import random
import socket
import sys
import time
import uuid

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Identity ────────────────────────────────────────────────────────────────
RUN_TAG = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"

# ── Config from env ──────────────────────────────────────────────────────────
STORAGE_STATE_PATH = "x_storage_state.json"
CSV_PATH = os.environ.get("POSTS_CSV_PATH", "table.csv")
CAPTION_SOURCE = os.environ.get("CAPTION_SOURCE", "csv").strip().lower()
CUSTOM_CAPTION_RAW = os.environ.get("CUSTOM_CAPTION", "")
SHUFFLE = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "0"))  # 0 = unlimited


# ── Google Drive helpers ─────────────────────────────────────────────────────

def get_env(name, required=True):
    value = os.getenv(name)
    if value is None:
        if required:
            sys.exit(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_drive_service():
    raw = get_env("GOOGLE_CREDENTIALS_JSON")
    info = json.loads(raw)
    creds = Credentials(
        token=info.get("access_token"),
        refresh_token=info["refresh_token"],
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def claim_file(service, file_id, current_name):
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed_name}).execute()
    check = service.files().get(fileId=file_id, fields="id,name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on '{current_name}'; skipping.")
        return None
    return claimed_name


def release_claim(service, file_id, original_name):
    try:
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}'.")
    except Exception as e:
        print(f"Warning: could not release claim on {file_id}: {e}")


def fetch_video_from_drive():
    """
    Returns (file_meta_dict, local_path) or (None, None) if no videos left.
    Unlike before, returns (None, None) instead of sys.exit() so the
    scheduler loop can handle an empty queue gracefully.
    """
    service = get_drive_service()
    folder_id = get_env("UPLOAD_FOLDER_ID")

    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime asc",
        pageSize=20,
        fields="files(id,name,mimeType)",
    ).execute()

    files = results.get("files", [])
    if not files:
        print("No files found in the upload folder.")
        return None, None

    if SHUFFLE:
        random.shuffle(files)

    for file in files:
        name = file["name"]
        mime = file.get("mimeType", "")

        if name.startswith(CLAIM_PREFIX):
            print(f"Skipping '{name}' — already claimed.")
            continue
        if not mime.startswith("video/"):
            print(f"Skipping '{name}' — not a video ({mime}).")
            continue

        claimed = claim_file(service, file["id"], name)
        if claimed is None:
            continue

        print(f"Claimed '{name}' as '{claimed}'. Downloading…")
        local_path = f"/tmp/{name}"
        data = service.files().get_media(fileId=file["id"]).execute()
        with open(local_path, "wb") as f:
            f.write(data)

        file["original_name"] = name
        file["claimed_name"] = claimed
        file["_service"] = service
        return file, local_path

    print("No unclaimed video files found in the upload folder.")
    return None, None


def move_to_processed(service, file_id, original_name):
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")
    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body={"name": original_name},
    ).execute()
    print(f"Moved '{original_name}' to processed folder.")


# ── Caption helpers ──────────────────────────────────────────────────────────

def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    return rows


def build_text_csv(row):
    action = row.get("Action Caption", "").strip()
    caption = row.get("Caption", "").strip()
    hashtags = row.get("Hashtags", "").strip()
    parts = [p for p in [action, caption, "", hashtags] if p is not None]
    return "\n".join(parts)


def build_text_custom(raw):
    return raw.replace("\\n", "\n").strip()


# ── Playwright helpers ───────────────────────────────────────────────────────

def wait_for_mask_gone(page, timeout=20000):
    try:
        page.wait_for_selector(
            '[data-testid="mask"]', state="hidden", timeout=timeout
        )
        print("Mask overlay gone.")
    except PWTimeout:
        print("Mask still present — force-removing via JS.")
        page.evaluate("""
            () => {
                const mask = document.querySelector('[data-testid="mask"]');
                if (mask) mask.remove();
                const layers = document.getElementById('layers');
                if (layers) layers.style.pointerEvents = 'none';
            }
        """)
        page.wait_for_timeout(500)


def js_focus_and_type(page, text):
    page.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (el) {
                el.focus();
                el.click();
            }
        }
    """)
    page.wait_for_timeout(500)

    for char in text:
        if char == "\n":
            page.keyboard.press("Enter")
        else:
            page.keyboard.type(char, delay=20)


# ── X / Playwright posting ───────────────────────────────────────────────────

def post_video_to_x(local_path, caption_text):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── 1. Load home first ────────────────────────────────────────────
        print("Loading X home…")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)

        if "login" in page.url:
            sys.exit(
                "Session expired (redirected to login). "
                "Re-run your session capture script and refresh X_STORAGE_STATE_JSON."
            )

        page.wait_for_timeout(3000)
        wait_for_mask_gone(page, timeout=15000)

        # ── 2. Open compose ───────────────────────────────────────────────
        print("Navigating to compose…")
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        wait_for_mask_gone(page, timeout=20000)

        # ── 3. Type caption ───────────────────────────────────────────────
        print("Locating textbox…")
        page.wait_for_selector(
            '[data-testid="tweetTextarea_0"]', state="attached", timeout=15000
        )
        page.wait_for_timeout(1000)
        js_focus_and_type(page, caption_text)
        print(f"Caption typed ({len(caption_text)} chars).")
        page.wait_for_timeout(500)

        # ── 4. Attach video ───────────────────────────────────────────────
        print("Attaching video…")
        file_input = page.locator('input[data-testid="fileInput"]').first
        try:
            file_input.wait_for(state="attached", timeout=8000)
        except PWTimeout:
            try:
                page.evaluate("""
                    () => {
                        const btn = document.querySelector('[data-testid="attachments"]');
                        if (btn) btn.click();
                    }
                """)
                page.wait_for_timeout(1000)
                file_input = page.locator('input[type="file"]').first
                file_input.wait_for(state="attached", timeout=8000)
            except PWTimeout:
                sys.exit("Could not find file input for video attachment.")

        file_input.set_input_files(local_path)
        print("Video attached. Waiting for upload to complete…")

        # ── 5. Wait for upload ────────────────────────────────────────────
        try:
            page.wait_for_selector(
                '[data-testid="progressBar"]', state="visible", timeout=20000
            )
            print("Upload started (progress bar visible).")
            page.wait_for_selector(
                '[data-testid="progressBar"]', state="detached", timeout=300000
            )
            print("Upload complete (progress bar gone).")
        except PWTimeout:
            print("Warning: progress bar not detected or timed out; continuing.")

        page.wait_for_timeout(5000)
        wait_for_mask_gone(page, timeout=15000)

        # ── 6. Submit post ────────────────────────────────────────────────
        print("Submitting post…")
        try:
            page.wait_for_selector(
                '[data-testid="tweetButton"]:not([aria-disabled="true"])',
                state="attached",
                timeout=15000,
            )
        except PWTimeout:
            print("Warning: tweetButton disabled check timed out; trying anyway.")

        page.wait_for_timeout(1000)

        clicked = page.evaluate("""
            () => {
                const btn = document.querySelector('[data-testid="tweetButton"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }
        """)

        if not clicked:
            sys.exit("Post button not found in DOM.")

        print("Post button clicked.")

        # ── 7. Confirm submission ─────────────────────────────────────────
        try:
            page.wait_for_url(
                lambda url: "/home" in url or "/compose" not in url,
                timeout=20000,
            )
            print("Post submitted successfully — navigated away from compose.")
        except PWTimeout:
            print("No navigation detected; checking for compose closure…")
            try:
                page.wait_for_selector(
                    '[data-testid="tweetTextarea_0"]', state="detached", timeout=5000
                )
                print("Compose textarea gone — post likely submitted.")
            except PWTimeout:
                print("Warning: could not confirm submission. Manual check advised.")

        page.wait_for_timeout(3000)
        browser.close()

    print("Posted to X successfully.")


# ── Single post cycle ────────────────────────────────────────────────────────

def run_one_post():
    """
    Fetch one video, build caption, post it, move to processed.
    Returns True if a post was made, False if no videos were available.
    """
    file_meta, local_path = fetch_video_from_drive()
    if file_meta is None:
        return False

    service = file_meta["_service"]
    original_name = file_meta["original_name"]
    file_id = file_meta["id"]

    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            release_claim(service, file_id, original_name)
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption = build_text_custom(CUSTOM_CAPTION_RAW)
    else:
        rows = load_caption_rows(CSV_PATH)
        caption = build_text_csv(random.choice(rows))

    print(f"\nCaption:\n{caption}\n")
    print(f"Video: {local_path}\n")

    try:
        post_video_to_x(local_path, caption)
    except Exception as e:
        print(f"Posting failed: {e}")
        release_claim(service, file_id, original_name)
        raise

    move_to_processed(service, file_id, original_name)

    try:
        os.remove(local_path)
    except OSError:
        pass

    return True


# ── Scheduler loop ───────────────────────────────────────────────────────────

def sleep_with_countdown(seconds):
    """Sleep for `seconds`, printing a countdown every 60s."""
    interval = 60
    remaining = seconds
    while remaining > 0:
        chunk = min(interval, remaining)
        mins = remaining // 60
        print(f"  Next post in ~{mins} minute(s)… (sleeping {chunk}s)")
        time.sleep(chunk)
        remaining -= chunk


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Write X session state from secret
    state_json = get_env("X_STORAGE_STATE_JSON")
    with open(STORAGE_STATE_PATH, "w") as f:
        f.write(state_json)

    interval_seconds = INTERVAL_MINUTES * 60
    post_count = 0

    print(f"Scheduler started — posting every {INTERVAL_MINUTES} minute(s).")
    if MAX_POSTS:
        print(f"Will stop after {MAX_POSTS} post(s).")
    else:
        print("Running until no videos remain or job timeout is reached.")

    while True:
        print(f"\n{'='*50}")
        print(f"Post #{post_count + 1} starting at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        print(f"{'='*50}")

        posted = run_one_post()

        if not posted:
            print("No videos left in upload folder. Exiting scheduler.")
            break

        post_count += 1
        print(f"Post #{post_count} done.")

        if MAX_POSTS and post_count >= MAX_POSTS:
            print(f"Reached MAX_POSTS={MAX_POSTS}. Exiting scheduler.")
            break

        # Sleep until next post
        print(f"\nSleeping {INTERVAL_MINUTES} minute(s) before next post…")
        sleep_with_countdown(interval_seconds)

    print(f"\nScheduler finished. Total posts made: {post_count}")


if __name__ == "__main__":
    main()
