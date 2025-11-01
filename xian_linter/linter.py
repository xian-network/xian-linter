import asyncio
import ast
import re

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List, Optional, Set
from pydantic import BaseModel
from io import StringIO
from pyflakes.api import check
from pyflakes.reporter import Reporter
from .custom import Linter


class Settings:
    """Simple settings class"""

    def __init__(self):
        self.MAX_CODE_SIZE: int = 1_000_000  # 1MB
        self.CACHE_SIZE: int = 100
        self.DEFAULT_WHITELIST_PATTERNS: frozenset = frozenset({
            'export', 'construct', 'Hash', 'Variable', 'ctx', 'now',
            'random', 'ForeignHash', 'ForeignVariable', 'block_num',
            'block_hash', 'importlib', 'hashlib', 'datetime', 'crypto',
            'decimal', 'Any', 'LogEvent', 'chain_id'
        })


settings = Settings()


class LintingException(Exception):
    """Custom exception for linting errors"""
    pass


@dataclass(slots=True)
class Position:
    """Represents a position in the source code"""
    line: int  # 0-based line number
    column: int  # 0-based column number


@dataclass(slots=True)
class LintError:
    """Standardized lint error format"""
    message: str
    severity: str = "error"
    position: Optional[Position] = None

    def to_dict(self) -> dict:
        result = {
            "message": self.message,
            "severity": self.severity
        }
        if self.position is not None:
            result["position"] = {
                "line": self.position.line,
                "column": self.position.column
            }
        return result


class Position_Model(BaseModel):
    line: int
    column: int


class LintError_Model(BaseModel):
    message: str
    severity: str
    position: Optional[Position_Model] = None


class LintResponse(BaseModel):
    success: bool
    errors: List[LintError_Model]


# Compile regex patterns once
PYFLAKES_PATTERN = re.compile(r'<string>:(\d+):(\d+):\s*(.+)')
CONTRACTING_PATTERN = re.compile(r'Line (\d+):\s*(.+)')


def standardize_error_message(message: str) -> str:
    """Standardize error message by removing extra location information."""
    # Remove (<unknown>, line X) pattern from the end
    location_pattern = r'\s*\(<unknown>,\s*line\s*\d+\)$'
    message = re.sub(location_pattern, '', message)
    return message


def is_duplicate_error(error1: LintError, error2: LintError) -> bool:
    """Check if two errors are duplicates by comparing standardized messages and positions."""
    msg1 = standardize_error_message(error1.message)
    msg2 = standardize_error_message(error2.message)

    # If messages are different, they're not duplicates
    if msg1 != msg2:
        return False

    # If one has position and other doesn't, they're not duplicates
    if bool(error1.position) != bool(error2.position):
        return False

    # If both have positions, compare them
    if error1.position and error2.position:
        return (error1.position.line == error2.position.line and
                error1.position.column == error2.position.column)

    # If neither has position, compare just messages
    return True


def deduplicate_errors(errors: List[LintError]) -> List[LintError]:
    """Remove duplicate errors while preserving order."""
    unique_errors = []
    for error in errors:
        # Standardize the message
        error.message = standardize_error_message(error.message)
        # Only add if not a duplicate
        if not any(is_duplicate_error(error, existing) for existing in unique_errors):
            unique_errors.append(error)
    return unique_errors


def parse_pyflakes_line(line: str, whitelist_patterns: Set[str]) -> Optional[LintError]:
    """Parse a Pyflakes error line into standardized format"""
    # Strip any "Pyflakes error: " prefix if present
    if line.startswith("Pyflakes error: "):
        line = line[len("Pyflakes error: "):]

    match = PYFLAKES_PATTERN.match(line)
    if not match:
        return None

    line_num, col, message = match.groups()

    if any(pattern in message for pattern in whitelist_patterns):
        return None

    return LintError(
        message=message,
        position=Position(
            line=int(line_num) - 1,
            column=int(col) - 1
        )
    )


def parse_contracting_line(violation: str) -> LintError:
    """Parse a Contracting linter error into standardized format"""
    # Strip any "Contracting linter error: " prefix if present
    if violation.startswith("Contracting linter error: "):
        violation = violation[len("Contracting linter error: "):]

    match = CONTRACTING_PATTERN.match(violation)
    if match:
        line_num = int(match.group(1))
        message = match.group(2)

        # Use the line number directly
        return LintError(
            message=message,
            position=Position(
                line=line_num,  # No subtraction
                column=0
            )
        )

    # Fallback for unmatched violations
    return LintError(message=violation)


async def run_pyflakes(code: str, whitelist_patterns: Set[str]) -> List[LintError]:
    """Runs Pyflakes and returns standardized errors"""
    try:
        loop = asyncio.get_event_loop()
        stdout = StringIO()
        stderr = StringIO()
        reporter = Reporter(stdout, stderr)

        await loop.run_in_executor(None, check, code, "<string>", reporter)

        combined_output = stdout.getvalue() + stderr.getvalue()
        errors = []

        for line in combined_output.splitlines():
            line = line.strip()
            if not line:
                continue

            error = parse_pyflakes_line(line, whitelist_patterns)
            if error:
                errors.append(error)

        return errors
    except Exception as e:
        raise LintingException(str(e)) from e


async def run_contracting_linter(code: str) -> List[LintError]:
    """Runs Contracting linter and returns standardized errors"""
    try:
        loop = asyncio.get_event_loop()
        tree = await loop.run_in_executor(None, ast.parse, code)
        linter = Linter()
        violations = await loop.run_in_executor(None, linter.check, tree)

        if not violations:
            return []

        return [
            parse_contracting_line(v.strip())
            for v in violations
            if v.strip()
        ]
    except Exception as e:
        # Extract line number from AST SyntaxError if available
        if isinstance(e, SyntaxError) and e.lineno is not None:
            return [LintError(
                message=str(e),
                position=Position(line=e.lineno - 1, column=e.offset - 1 if e.offset else 0)
            )]
        raise LintingException(str(e)) from e


@lru_cache(maxsize=settings.CACHE_SIZE)
def get_whitelist_patterns(patterns_str: Optional[str] = None) -> frozenset:
    """Convert whitelist patterns string to frozenset for caching"""
    if not patterns_str:
        return settings.DEFAULT_WHITELIST_PATTERNS
    return frozenset(patterns_str.split(","))


def lint_code_inline(code: str, whitelist_patterns: Iterable[str] | None = None, ) -> list[LintError_Model]:
    """Run the linters synchronously inside the current process.

    Args:
        code: Contract source to lint.
        whitelist_patterns: Optional iterable of substrings that should suppress
            matching Pyflakes diagnostics (mirrors the API exposed by the HTTP
            endpoints). When omitted the default whitelist is used.

    Returns:
        A list of LintError_Model instances describing the violations.
    """
    patterns = (
        frozenset(whitelist_patterns)
        if whitelist_patterns is not None
        else get_whitelist_patterns()
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        errors = loop.run_until_complete(lint_code(code, patterns))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)

    return [convert_lint_error_to_model(error) for error in errors]


async def lint_code(code: str, whitelist_patterns: Set[str]) -> List[LintError]:
    """Run all linters in parallel"""
    try:
        pyflakes_task = run_pyflakes(code, whitelist_patterns)
        contracting_task = run_contracting_linter(code)

        results = await asyncio.gather(pyflakes_task, contracting_task)
        all_errors = results[0] + results[1]

        # Deduplicate errors
        return deduplicate_errors(all_errors)
    except LintingException as e:
        error_msg = str(e)
        # Strip any known prefixes from the error message
        for prefix in ["Pyflakes error: ", "Contracting linter error: "]:
            if error_msg.startswith(prefix):
                error_msg = error_msg[len(prefix):]
                break
        return [LintError(message=error_msg)]


def convert_lint_error_to_model(error: LintError) -> LintError_Model:
    """Convert a LintError to a LintError_Model"""
    if error.position:
        position = Position_Model(
            line=error.position.line,
            column=error.position.column
        )
    else:
        position = None

    return LintError_Model(
        message=error.message,
        severity=error.severity,
        position=position
    )
