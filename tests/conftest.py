import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_approval.config import Settings  # noqa: E402
from agent_approval.service import build_service  # noqa: E402

SAMPLES = json.loads((ROOT / "samples" / "inbound.json").read_text())


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, log_json=False, retry_base_delay=0.01)


@pytest.fixture
def svc(settings):
    return build_service(settings, sleep=lambda s: None)


def sample(i: int, **over) -> dict:
    return SAMPLES[i] | over
