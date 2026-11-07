from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    policy_file: Path = Path("config/mock_policies.yaml")
    rules_file: Path = Path("config/rules.yaml")
    model: str = "claude-sonnet-5-5"
    complex_model: str = "claude-opus-5-5"
    effort: str = "high"
    max_coverage_turns: int = 8
    max_per_day: int = 2000
    max_attachments: int = 10
    max_attachment_bytes: int = 20 * 1024 * 1024

    @property
    def kill_switch(self) -> Path:
        return self.data_dir / "CLAIMS_DISABLED"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            data_dir=Path(env("CLAIMS_DATA_DIR", "data")),
            policy_file=Path(env("CLAIMS_POLICY_FILE", "config/mock_policies.yaml")),
            rules_file=Path(env("CLAIMS_RULES_FILE", "config/rules.yaml")),
            model=env("CLAIMS_MODEL", "claude-sonnet-5-5"),
            complex_model=env("CLAIMS_COMPLEX_MODEL", "claude-opus-5-5"),
            effort=env("CLAIMS_EFFORT", "high"),
            max_coverage_turns=int(env("CLAIMS_MAX_COVERAGE_TURNS", "8")),
            max_per_day=int(env("CLAIMS_MAX_PER_DAY", "2000")),
            max_attachments=int(env("CLAIMS_MAX_ATTACHMENTS", "10")),
            max_attachment_bytes=int(env("CLAIMS_MAX_ATTACHMENT_MB", "20")) * 1024 * 1024,
        )
