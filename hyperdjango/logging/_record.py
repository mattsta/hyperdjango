"""
Log record attribute types.

All record components are dataclasses with __format__ for use in format templates.
RecordException supports pickling for multiprocessing safety.
"""

import traceback as _traceback
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict

from hyperdjango.types import LogExtra


@dataclass(frozen=True)
class RecordLevel:
    """Log level in a record. Formats as name by default."""

    name: str
    no: int
    icon: str = ""

    def __str__(self):
        return self.name

    def __format__(self, spec):
        return format(self.name, spec)

    def __eq__(self, other):
        if isinstance(other, RecordLevel):
            return self.no == other.no
        if isinstance(other, str):
            return self.name == other
        if isinstance(other, int):
            return self.no == other
        return NotImplemented

    def __hash__(self):
        return hash(self.no)


@dataclass(frozen=True)
class RecordFile:
    """Source file info. Formats as basename by default."""

    name: str
    path: str

    def __str__(self):
        return self.name

    def __format__(self, spec):
        return format(self.name, spec)


@dataclass(frozen=True)
class RecordThread:
    """Thread info. Formats as ID by default."""

    id: int
    name: str

    def __str__(self):
        return str(self.id)

    def __format__(self, spec):
        return format(self.id, spec)


@dataclass(frozen=True)
class RecordProcess:
    """Process info. Formats as PID by default."""

    id: int
    name: str

    def __str__(self):
        return str(self.id)

    def __format__(self, spec):
        return format(self.id, spec)


@dataclass
class RecordException:
    """Exception info attached to a log record.

    Formats as full traceback string. Supports pickling by stripping
    the traceback object (which is not picklable).
    """

    type: type | None
    value: BaseException | None
    traceback: types.TracebackType | None

    def __str__(self):
        if self.type is None:
            return ""
        return "".join(
            _traceback.format_exception(self.type, self.value, self.traceback)
        )

    def __bool__(self):
        return self.type is not None

    def __reduce__(self):
        # Strip the (unpicklable) traceback object; `type` and `value` are
        # plain attributes and pickle directly. (If `value` itself is an
        # unpicklable exception that surfaces in the pickler, not here.)
        return (RecordException, (self.type, self.value, None))


# Empty singleton for records without exceptions
NO_EXCEPTION = RecordException(type=None, value=None, traceback=None)


class LogRecord(TypedDict, total=False):
    """Typed dict for log records passed through handlers and sinks."""

    level: RecordLevel
    file: RecordFile
    function: str
    line: int
    message: str
    module: str
    name: str
    thread: RecordThread
    process: RecordProcess
    time: datetime
    elapsed: timedelta
    exception: RecordException
    extra: LogExtra
