import json
import random
import time
from pathlib import Path

import yaml

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class Workspace:
    """One event week (e.g. SF_tech). Holds profile.yaml and a data/ dir."""

    def __init__(self, path):
        self.root = Path(path).resolve()
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.root / "profile.yaml"

    @property
    def profile(self):
        if not self.profile_path.exists():
            raise SystemExit(f"Missing {self.profile_path}; copy profile.example.yaml there and fill it in.")
        return yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))

    def load(self, name, default=None):
        p = self.data / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def save(self, name, obj):
        (self.data / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")

    @property
    def browser_profile(self):
        return self.root / ".browser-profile"


class Throttle:
    """Polite pacing: at least `min_s` seconds (plus jitter) between requests."""

    def __init__(self, min_s=3.0, jitter=1.5):
        self.min_s, self.jitter, self.last = min_s, jitter, 0.0

    def wait(self):
        gap = self.min_s + random.uniform(0, self.jitter)
        dt = time.monotonic() - self.last
        if dt < gap:
            time.sleep(gap - dt)
        self.last = time.monotonic()
