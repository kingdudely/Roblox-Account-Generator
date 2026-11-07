"""Account persistence — JSONL is the source of truth, CSV is the export."""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "username", "password", "birthday", "gender", "user_id", "display_name",
    "cookie", "status", "note", "proxy", "created_at", "captcha",
]


@dataclass
class AccountRecord:
    username: str
    password: str
    birthday: str
    gender: str
    user_id: int | None = None
    display_name: str | None = None
    cookie: str | None = None
    status: str = "created"      # created | failed | captcha_failed | taken | banned
    note: str = ""
    proxy: str | None = None
    captcha: str = "none"        # none | suppressed | api | manual
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict:
        return asdict(self)


class AccountStore:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.out_dir / "accounts.jsonl"
        self.csv_path = self.out_dir / "accounts.csv"
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._load_seen()

    def _load_seen(self) -> None:
        if not self.jsonl_path.exists():
            return
        for line in self.jsonl_path.read_text().splitlines():
            try:
                data = json.loads(line)
                if data.get("status") == "created" and data.get("username"):
                    self._seen.add(data["username"].lower())
            except json.JSONDecodeError:
                continue

    def is_taken(self, username: str) -> bool:
        return username.lower() in self._seen

    def append(self, record: AccountRecord) -> None:
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            if record.status == "created":
                self._seen.add(record.username.lower())
            write_header = not self.csv_path.exists()
            with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow({k: record.to_dict().get(k, "") for k in FIELDS})

    def credentials_file(self, path: Path | None = None) -> Path:
        """user:pass list of successfully created accounts."""
        path = path or (self.out_dir / "credentials.txt")
        lines = []
        if self.jsonl_path.exists():
            for line in self.jsonl_path.read_text().splitlines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("status") == "created":
                    lines.append(f"{data['username']}:{data['password']}")
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
        return path

    def cookies_file(self, path: Path | None = None) -> Path:
        path = path or (self.out_dir / "cookies.json")
        rows = []
        if self.jsonl_path.exists():
            for line in self.jsonl_path.read_text().splitlines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("status") == "created" and data.get("cookie"):
                    rows.append({
                        "username": data["username"],
                        "userId": data.get("user_id"),
                        ".ROBLOSECURITY": data["cookie"],
                    })
        path.write_text(json.dumps(rows, indent=2))
        return path
