from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = Path("data")
    log_json: bool = True
    public_url: str = "http://localhost:8800"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    llm_timeout: float = 30.0
    agent_max_steps: int = 4
    crm_provider: str = "mock"
    hubspot_token: str = ""
    sheets_provider: str = "mock"
    google_sheets_id: str = ""
    google_sheets_range: str = "Orders!A:G"
    google_api_key: str = ""
    kb_dir: Path = ROOT / "kb"
    samples_dir: Path = ROOT / "samples"
    slack_bot_token: str = ""
    slack_signing_secret: str = "demo-signing-secret"
    slack_channel: str = "#support-approvals"
    approver_user: str = ""
    approver_password: str = ""
    sender: str = "mock"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = "support@example.com"
    max_refund_without_manager: float = 100.0
    retry_attempts: int = 3
    retry_base_delay: float = 1.0

    @property
    def mock_llm(self) -> bool:
        return not self.openai_api_key

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"


def load_settings(**overrides) -> Settings:
    load_dotenv()
    e = os.environ.get
    s = Settings(
        data_dir=Path(e("DATA_DIR", "data")),
        log_json=e("LOG_JSON", "true").lower() == "true",
        public_url=e("PUBLIC_URL", "http://localhost:8800"),
        openai_api_key=e("OPENAI_API_KEY", ""),
        openai_base_url=e("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=e("OPENAI_MODEL", "gpt-4o-mini"),
        llm_timeout=float(e("LLM_TIMEOUT", "30")),
        agent_max_steps=int(e("AGENT_MAX_STEPS", "4")),
        crm_provider=e("CRM_PROVIDER", "mock"),
        hubspot_token=e("HUBSPOT_TOKEN", ""),
        sheets_provider=e("SHEETS_PROVIDER", "mock"),
        google_sheets_id=e("GOOGLE_SHEETS_ID", ""),
        google_sheets_range=e("GOOGLE_SHEETS_RANGE", "Orders!A:G"),
        google_api_key=e("GOOGLE_API_KEY", ""),
        kb_dir=Path(e("KB_DIR", str(ROOT / "kb"))),
        slack_bot_token=e("SLACK_BOT_TOKEN", ""),
        slack_signing_secret=e("SLACK_SIGNING_SECRET", "") or "demo-signing-secret",
        slack_channel=e("SLACK_APPROVAL_CHANNEL", "#support-approvals"),
        approver_user=e("APPROVER_USER", ""),
        approver_password=e("APPROVER_PASSWORD", ""),
        sender=e("SENDER", "mock"),
        smtp_host=e("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(e("SMTP_PORT", "587")),
        smtp_user=e("SMTP_USER", ""),
        smtp_password=e("SMTP_PASSWORD", ""),
        from_address=e("FROM_ADDRESS", "support@example.com"),
        max_refund_without_manager=float(e("MAX_REFUND_WITHOUT_MANAGER", "100")),
        retry_attempts=int(e("RETRY_ATTEMPTS", "3")),
        retry_base_delay=float(e("RETRY_BASE_DELAY", "1.0")),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
