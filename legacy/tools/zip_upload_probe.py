#!/usr/bin/env python3
"""Does the upload endpoint accept an archive, and can the model see inside it?

The 18-upload budget is the hard limit on how many waves one anonymous ChatGPT
identity can serve. The obvious cheat is to zip ten wave images into one file, so
one upload carries a whole challenge.

"Accepts it" and "the model can see it" are different questions, and only the
second one matters. This reports every stage separately so a failure says *where*
the archive path dies:

    1. POST /backend-anon/files             file registration
    2. PUT  <upload_url>                    blob upload
    3. POST /files/process_upload_stream    "Succeeded processing"?
    4. one conversation turn                can it read a number off the images

    ./tools/zip_upload_probe.py --wave debug/vision/20261029-084526-unknown-1
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chatgpt_anon import BASE, AnonChat  # noqa: E402


def stage(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {label:<30} {'OK  ' if ok else 'FAIL'}  {detail[:130]}", flush=True)
    return ok


def upload_archive(chat: AnonChat, archive: Path) -> dict | None:
    data = archive.read_bytes()
    name = archive.name
    mime = mimetypes.guess_type(name)[0] or "application/zip"

    chat._headers("REQUIREMENTS")
    r = chat.session.post(f"{BASE}/backend-anon/files", json={
        "file_name": name, "file_size": len(data), "use_case": "multimodal",
        "timezone_offset_min": 0, "reset_rate_limits": False,
    }, timeout=chat.timeout)
    if not stage("1  file registration", r.status_code == 200, r.text[:130]):
        return None
    j = r.json()
    file_id, upload_url = j.get("file_id"), j.get("upload_url")
    if not stage("1b file_id + upload_url", bool(file_id and upload_url),
                 str(j)[:130]):
        return None

    chat._headers("FILE")
    chat.session.headers["content-type"] = mime
    up = chat.session.put(upload_url, data=data, timeout=chat.timeout)
    if not stage("2  blob PUT", up.status_code in (200, 201),
                 f"{up.status_code} {up.text[:110]}"):
        return None

    chat._headers("REQUIREMENTS")
    pr = chat.session.post(f"{BASE}/backend-anon/files/process_upload_stream",
                           json={"file_id": file_id, "use_case": "multimodal",
                                 "index_for_retrieval": False, "file_name": name},
                           timeout=chat.timeout)
    if not stage("3  process_upload_stream", "Succeeded processing" in (pr.text or ""),
                 pr.text[:150]):
        return None
    return {"file_id": file_id, "name": name, "mime": mime,
            "size": len(data), "width": 0, "height": 0}


PROMPT = ("I attached an archive. It contains PNG images numbered 01, 02, 03. "
          "Look inside it and tell me the number shown in the first image. "
          "If you cannot see any images inside it, reply exactly NO IMAGES.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=Path, required=True)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/zip"))
    args = ap.parse_args()

    images = sorted(args.wave.glob("*.img")) or sorted(args.wave.glob("*.png"))
    if not images:
        print(f"  no images in {args.wave}")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    archive = args.out / "wave.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        for i, img in enumerate(images[:10], start=1):
            zf.write(img, arcname=f"{i:02d}{img.suffix or '.png'}")
    print(f"  archive {archive.name}: {len(images[:10])} images, "
          f"{archive.stat().st_size // 1024} KB\n")

    chat = AnonChat(args.proxy)
    print(f"  identity {chat.device_id[:8]}  tz {chat.tz_name}\n")

    record = upload_archive(chat, archive)
    if record is None:
        print("\n  archive rejected at the upload layer — zip is not a path")
        return 1

    try:
        reply = chat.ask_uploaded(PROMPT, [record], ["ARCHIVE:"])
    except Exception as exc:
        stage("4  model turn", False, f"{type(exc).__name__}: {exc}")
        return 1

    text = (reply.get("text") or "").strip()
    stage("4  model turn", bool(text), text[:160])
    sees = bool(text) and "NO IMAGES" not in text.upper()
    print(f"\n  model {'CAN' if sees else 'CANNOT'} see inside the archive")
    print(f"  reply: {text[:300]!r}")
    print(f"  conversation uploads counted: {len(reply.get('upload') or [])}")
    return 0 if sees else 1


if __name__ == "__main__":
    raise SystemExit(main())
