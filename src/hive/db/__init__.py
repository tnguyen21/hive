from .core import DatabaseCore, SCHEMA, ALLOWED_TAGS, validate_tags
from .issues import IssuesMixin
from .notes import NotesMixin
from .metrics import MetricsMixin


class Database(IssuesMixin, NotesMixin, MetricsMixin, DatabaseCore):
    pass


__all__ = ["Database", "validate_tags", "ALLOWED_TAGS", "SCHEMA"]
