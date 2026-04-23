"""Confirm all touched modules still import cleanly."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.core import server  # noqa: F401
from pulse.core import brain  # noqa: F401
from pulse.core import prompt_contract  # noqa: F401
from pulse.core import event_types  # noqa: F401
from pulse.core import promotion  # noqa: F401
from pulse.core import module as module_mod  # noqa: F401
from pulse.core import router as intent_router_mod  # noqa: F401
from pulse.core.llm import router  # noqa: F401
from pulse.core.memory import core_memory  # noqa: F401
from pulse.core.memory import workspace_memory  # noqa: F401
from pulse.core.learning import preference_extractor  # noqa: F401
from pulse.core.learning import domain_preference_dispatcher  # noqa: F401
from pulse.core.soul import evolution  # noqa: F401
from pulse.modules.job import preference_applier  # noqa: F401
from pulse.modules.job.profile import module as job_profile_module  # noqa: F401
from pulse.modules.job.greet import service as job_greet_service  # noqa: F401
from pulse.modules.job.greet import repository as job_greet_repository  # noqa: F401
from pulse.modules.job.greet import module as job_greet_module  # noqa: F401

print("[smoke-imports] OK — all touched modules import cleanly")
