"""Intel topic configurations.

Each ``<topic_id>.yaml`` declares one user-visible information theme.
Adding a topic is a YAML change, never a code change. The module loads
every ``*.yaml`` here at startup, validates against ``TopicConfig`` and
registers a patrol per topic. Files prefixed with ``_`` are templates
only and are ignored at load time.

See ``../docs/adding-a-topic.md`` for the authoring tutorial and
``_schema.py`` for the canonical pydantic schema.
"""

from ._schema import (
    DiversityConfig,
    MemoryConfig,
    PublishConfig,
    ScoringConfig,
    SourceConfig,
    TopicConfig,
    discover_topic_files,
    load_topic_configs,
    load_topic_file,
)

__all__ = [
    "DiversityConfig",
    "MemoryConfig",
    "PublishConfig",
    "ScoringConfig",
    "SourceConfig",
    "TopicConfig",
    "discover_topic_files",
    "load_topic_configs",
    "load_topic_file",
]
