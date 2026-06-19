"""
Fetches a video from Google Drive (folder: UPLOAD_FOLDER_ID),
posts it to X with a caption from table.csv, then moves the file
to PROCESSED_FOLDER_ID to avoid re-posting.

Google credentials are stored as a GitHub secret (GOOGLE_CREDENTIALS_JSON)
instead of token.pickle. A refresh token is embedded in the same JSON so
no browser interaction is needed at runtime.

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


# ── Google Drive helpers ─────────────────────────────────────────────────────

def get_env(name, required=True):
    value = os.getenv(name)
    if value is None:
        if required:
            sys.exit(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_drive_service():
    """
    Build a Drive service from GOOGLE_CREDENTIALS_JSON secret.

    The JSON must contain:
        client_id, client_secret, refresh_token, token_uri

    Generate it once locally with generate_token.py (see README), then store
    the printed JSON as the GOOGLE_CREDENTIALS_JSON GitHub secret.
    """
    raw = get_env("GOOGLE_CREDENTIALS_JSON")
    info = json.loads(raw)

    creds = Credentials(
        token=info.get("access_token"),          # may be None / expired
        refresh_token=info["refresh_token"],
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    # Refresh unconditionally so we always have a valid access token.
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def claim_file(service, file_id, current_name):
    """Atomically claim a file by renaming it; returns claimed name or None."""
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed_name}).execute()
    check = service.files().get(fileId=file_id, fields="id,name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on '{current_name}'; skipping.")
        return None
    return claimed_name


def release_claim(service, file_id, original_name):
    """Rename file back to original if posting failed after claiming."""
    try:
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}'.")
    except Exception as e:
        print(f"Warning: could not release claim on {file_id}: {e}")


def fetch_video_from_drive():
    """
    List videos in UPLOAD_FOLDER_ID, claim one, download it to /tmp.
    Returns (file_meta_dict, local_path) or (None, None).
    """
    service = get_drive_service()
    folder_id = get_env("UPLOAD_FOLDER_ID")

    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime asc",          # oldest first = FIFO queue
        pageSize=20,
        fields="files(id,name,mimeType)",
    ).execute()

    files = results.get("files", [])
    if not files:
        sys.exit("No files found in the upload folder.")

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
        file["_service"] = service          # reuse authenticated client
        return file, local_path

    sys.exit("No unclaimed video files found in the upload folder.")


def move_to_processed(service, file_id, original_name):
    """Move file from UPLOAD_FOLDER_ID to PROCESSED_FOLDER_ID."""
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")
    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body={"name": original_name},       # restore clean name
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


# ── X / Playwright posting ───────────────────────────────────────────────────

def post_video_to_x(local_path, caption_text):
    """
    Open X compose, attach the video file, fill caption, and submit.
    Uses a saved Playwright storage state for authentication.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            # Desktop viewport — X's video upload button is easier to target
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        page.goto("https://x.com/compose/post", wait_until="domcontentloaded")

        if "login" in page.url:
            sys.exit(
                "Session expired (redirected to login). "
                "Re-run your session capture script and refresh X_STORAGE_STATE_JSON."
            )

        # Fill caption text
        textbox = page.get_by_test_id("tweetTextarea_0")
        textbox.wait_for(state="visible", timeout=15000)
        textbox.click()
        textbox.fill(caption_text)

        # Attach video via the hidden file input
        file_input = page.locator('input[data-testid="fileInput"]').first
        # If not found, X may render it differently — wait a moment
        try:
            file_input.wait_for(state="attached", timeout=8000)
        except PWTimeout:
            # Fallback: click the media button to reveal the input
            page.get_by_test_id("attachments").click()
            file_input = page.locator('input[type="file"]').first
            file_input.wait_for(state="attached", timeout=8000)

        file_input.set_input_files(local_path)
        print("Video attached. Waiting for upload to complete…")

        # Wait for upload progress to disappear (X shows a progress bar)
        try:
            page.wait_for_selector(
                '[data-testid="progressBar"]', state="detached", timeout=120000
            )
        except PWTimeout:
            print("Warning: upload progress bar timeout; posting anyway.")

        # Small extra wait to let X process the video
        page.wait_for_timeout(3000)

        # Submit
        post_button = page.get_by_test_id("tweetButton")
        post_button.wait_for(state="visible", timeout=10000)
        post_button.click()
        page.wait_for_timeout(5000)

        browser.close()
    print("Posted to X successfully.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Write X session state from secret
    state_json = get_env("X_STORAGE_STATE_JSON")
    with open(STORAGE_STATE_PATH, "w") as f:
        f.write(state_json)

    # Fetch video from Drive
    file_meta, local_path = fetch_video_from_drive()
    service = file_meta["_service"]
    original_name = file_meta["original_name"]
    file_id = file_meta["id"]

    # Build caption
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

    # Post to X
    try:
        post_video_to_x(local_path, caption)
    except Exception as e:
        print(f"Posting failed: {e}")
        release_claim(service, file_id, original_name)
        raise

    # Move to processed folder
    move_to_processed(service, file_id, original_name)

    # Clean up local temp file
    try:
        os.remove(local_path)
    except OSError:
        pass

    print("Done.")


if __name__ == "__main__":
    main()
