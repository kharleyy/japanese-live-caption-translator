from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

TranslationStatus = Literal["translation_pending", "translation_done", "translation_failed"]

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def parse_utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_iso(value: datetime | str | None = None) -> str:
    return parse_utc(value).isoformat(timespec="milliseconds")


def shanghai_business_date(value: datetime | str | None = None) -> str:
    local_value = parse_utc(value).astimezone(SHANGHAI_TIMEZONE)
    return local_value.date().isoformat()


def validate_business_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("business_date must use YYYY-MM-DD") from error


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: int
    business_date: str
    name: str
    started_at_utc: str
    ended_at_utc: str | None


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    id: int
    session_id: int
    source_ja: str
    translation_zh: str | None
    finalized_at_utc: str
    translation_status: TranslationStatus
    retry_count: int
    last_error: str | None
    next_retry_at_utc: str | None
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ContextSegment:
    source_ja: str
    translation_zh: str


@dataclass(frozen=True, slots=True)
class TranslationUsageSummary:
    requests: int
    successes: int
    failures: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float


@dataclass(frozen=True, slots=True)
class DayDeletionResult:
    business_date: str
    deleted_sessions: int
    deleted_segments: int


class Database:
    """Local SQLite storage for sessions and finalized caption segments."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize_schema()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        if self._connection:
            self._connection.close()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_date TEXT NOT NULL,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                source_ja TEXT NOT NULL CHECK (length(trim(source_ja)) > 0),
                translation_zh TEXT,
                finalized_at_utc TEXT NOT NULL,
                translation_status TEXT NOT NULL DEFAULT 'translation_pending'
                    CHECK (translation_status IN (
                        'translation_pending',
                        'translation_done',
                        'translation_failed'
                    )),
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                last_error TEXT,
                next_retry_at_utc TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0)
            );

            CREATE TABLE IF NOT EXISTS translation_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL
                    REFERENCES segments(id) ON DELETE CASCADE,
                attempted_at_utc TEXT NOT NULL,
                success INTEGER NOT NULL CHECK (success IN (0, 1)),
                error_code TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
                estimated_cost REAL NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_business_date
                ON sessions(business_date, started_at_utc);
            CREATE INDEX IF NOT EXISTS idx_segments_session_time
                ON segments(session_id, finalized_at_utc, id);
            CREATE INDEX IF NOT EXISTS idx_segments_translation_queue
                ON segments(translation_status, finalized_at_utc, id);
            CREATE INDEX IF NOT EXISTS idx_translation_attempts_time
                ON translation_attempts(attempted_at_utc, id);
            """
        )
        segment_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(segments)")
        }
        if "next_retry_at_utc" not in segment_columns:
            self._connection.execute(
                "ALTER TABLE segments ADD COLUMN next_retry_at_utc TEXT"
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_segments_translation_retry
            ON segments(translation_status, next_retry_at_utc, finalized_at_utc, id)
            """
        )
        self._connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def create_session(
        self,
        name: str,
        *,
        started_at: datetime | str | None = None,
        business_date: str | date | None = None,
    ) -> SessionRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("session name cannot be empty")
        started_at_utc = utc_iso(started_at)
        resolved_date = (
            validate_business_date(business_date)
            if business_date is not None
            else shanghai_business_date(started_at_utc)
        )
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions(business_date, name, started_at_utc)
                VALUES (?, ?, ?)
                """,
                (resolved_date, clean_name, started_at_utc),
            )
            session_id = int(cursor.lastrowid)
        return self.get_session(session_id)

    def get_or_create_open_session(
        self,
        *,
        name: str | None = None,
        started_at: datetime | str | None = None,
    ) -> SessionRecord:
        started_at_utc = utc_iso(started_at)
        business_date = shanghai_business_date(started_at_utc)
        row = self._connection.execute(
            """
            SELECT *
            FROM sessions
            WHERE business_date = ? AND ended_at_utc IS NULL
            ORDER BY started_at_utc DESC, id DESC
            LIMIT 1
            """,
            (business_date,),
        ).fetchone()
        if row is not None:
            return self._session_from_row(row)

        local_time = parse_utc(started_at_utc).astimezone(SHANGHAI_TIMEZONE)
        session_name = name or f"直播 {local_time:%Y-%m-%d %H:%M}"
        return self.create_session(
            session_name,
            started_at=started_at_utc,
            business_date=business_date,
        )

    def get_session(self, session_id: int) -> SessionRecord:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session {session_id} does not exist")
        return self._session_from_row(row)

    def close_session(
        self,
        session_id: int,
        *,
        ended_at: datetime | str | None = None,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET ended_at_utc = ? WHERE id = ?",
                (utc_iso(ended_at), session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"session {session_id} does not exist")

    def list_sessions(self, business_date: str | date) -> list[SessionRecord]:
        resolved_date = validate_business_date(business_date)
        rows = self._connection.execute(
            """
            SELECT *
            FROM sessions
            WHERE business_date = ?
            ORDER BY started_at_utc, id
            """,
            (resolved_date,),
        ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def add_finalized_segment(
        self,
        session_id: int,
        source_ja: str,
        *,
        finalized_at: datetime | str | None = None,
        translation_status: TranslationStatus = "translation_pending",
    ) -> SegmentRecord:
        clean_source = " ".join(source_ja.split()).strip()
        if not clean_source:
            raise ValueError("source_ja cannot be empty")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO segments(
                    session_id,
                    source_ja,
                    finalized_at_utc,
                    translation_status
                )
                VALUES (?, ?, ?, ?)
                """,
                (session_id, clean_source, utc_iso(finalized_at), translation_status),
            )
            segment_id = int(cursor.lastrowid)
        return self.get_segment(segment_id)

    def add_finalized_segments(
        self,
        session_id: int,
        sources: list[tuple[str, datetime | str | None]],
    ) -> list[SegmentRecord]:
        segment_ids: list[int] = []
        with self.transaction() as connection:
            for source_ja, finalized_at in sources:
                clean_source = " ".join(source_ja.split()).strip()
                if not clean_source:
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO segments(session_id, source_ja, finalized_at_utc)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, clean_source, utc_iso(finalized_at)),
                )
                segment_ids.append(int(cursor.lastrowid))
        return [self.get_segment(segment_id) for segment_id in segment_ids]

    def get_segment(self, segment_id: int) -> SegmentRecord:
        row = self._connection.execute(
            "SELECT * FROM segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"segment {segment_id} does not exist")
        return self._segment_from_row(row)

    def list_segments_for_date(
        self,
        business_date: str | date,
        *,
        after_id: int = 0,
    ) -> list[SegmentRecord]:
        resolved_date = validate_business_date(business_date)
        if after_id < 0:
            raise ValueError("after_id cannot be negative")
        rows = self._connection.execute(
            """
            SELECT segments.*
            FROM segments
            JOIN sessions ON sessions.id = segments.session_id
            WHERE sessions.business_date = ?
              AND segments.id > ?
            ORDER BY segments.finalized_at_utc, segments.id
            """,
            (resolved_date, after_id),
        ).fetchall()
        return [self._segment_from_row(row) for row in rows]

    def get_latest_segment_id_for_date(
        self,
        business_date: str | date,
    ) -> int:
        resolved_date = validate_business_date(business_date)
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(segments.id), 0)
            FROM segments
            JOIN sessions ON sessions.id = segments.session_id
            WHERE sessions.business_date = ?
            """,
            (resolved_date,),
        ).fetchone()
        return int(row[0])

    def list_pending_translations(
        self,
        limit: int = 100,
        *,
        due_at: datetime | str | None = None,
    ) -> list[SegmentRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        due_at_utc = utc_iso(due_at)
        rows = self._connection.execute(
            """
            SELECT *
            FROM segments
            WHERE translation_status = 'translation_pending'
              AND (
                  next_retry_at_utc IS NULL
                  OR next_retry_at_utc <= ?
              )
            ORDER BY finalized_at_utc, id
            LIMIT ?
            """,
            (due_at_utc, limit),
        ).fetchall()
        return [self._segment_from_row(row) for row in rows]

    def get_next_pending_translation(self) -> SegmentRecord | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM segments
            WHERE translation_status = 'translation_pending'
            ORDER BY finalized_at_utc, id
            LIMIT 1
            """
        ).fetchone()
        return self._segment_from_row(row) if row is not None else None

    def get_recent_translation_context(
        self,
        segment_id: int,
        *,
        limit: int = 6,
        after_id: int = 0,
    ) -> list[ContextSegment]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if after_id < 0:
            raise ValueError("after_id cannot be negative")
        segment = self.get_segment(segment_id)
        rows = self._connection.execute(
            """
            SELECT source_ja, translation_zh
            FROM segments
            WHERE session_id = ?
              AND id < ?
              AND id > ?
              AND translation_status = 'translation_done'
              AND translation_zh IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (segment.session_id, segment_id, after_id, limit),
        ).fetchall()
        return [
            ContextSegment(
                source_ja=str(row["source_ja"]),
                translation_zh=str(row["translation_zh"]),
            )
            for row in reversed(rows)
        ]

    def mark_translation_done(
        self,
        segment_id: int,
        translation_zh: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost: float = 0.0,
        attempted_at: datetime | str | None = None,
    ) -> None:
        clean_translation = translation_zh.strip()
        if not clean_translation:
            raise ValueError("translation_zh cannot be empty")
        if input_tokens < 0 or output_tokens < 0 or estimated_cost < 0:
            raise ValueError("token counts and estimated cost cannot be negative")
        attempted_at_utc = utc_iso(attempted_at)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE segments
                SET translation_zh = ?,
                    translation_status = 'translation_done',
                    last_error = NULL,
                    next_retry_at_utc = NULL,
                    input_tokens = ?,
                    output_tokens = ?
                WHERE id = ?
                """,
                (clean_translation, input_tokens, output_tokens, segment_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"segment {segment_id} does not exist")
            self._insert_translation_attempt(
                connection,
                segment_id=segment_id,
                attempted_at_utc=attempted_at_utc,
                success=True,
                error_code=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
            )

    def mark_translation_failed(
        self,
        segment_id: int,
        error_message: str,
        *,
        retryable: bool,
        error_code: str = "translation_error",
        next_retry_at: datetime | str | None = None,
        attempted_at: datetime | str | None = None,
    ) -> None:
        clean_error = error_message.strip() or "translation failed"
        clean_code = error_code.strip() or "translation_error"
        status: TranslationStatus = (
            "translation_pending" if retryable else "translation_failed"
        )
        attempted_at_utc = utc_iso(attempted_at)
        next_retry_at_utc = utc_iso(next_retry_at) if retryable else None
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE segments
                SET translation_status = ?,
                    retry_count = retry_count + 1,
                    last_error = ?,
                    next_retry_at_utc = ?
                WHERE id = ?
                """,
                (status, clean_error[:2000], next_retry_at_utc, segment_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"segment {segment_id} does not exist")
            self._insert_translation_attempt(
                connection,
                segment_id=segment_id,
                attempted_at_utc=attempted_at_utc,
                success=False,
                error_code=clean_code[:100],
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
            )

    def translation_usage_summary(
        self,
        business_date: str | date | None = None,
    ) -> TranslationUsageSummary:
        query = """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(translation_attempts.success), 0) AS successes,
                COALESCE(SUM(
                    CASE WHEN translation_attempts.success = 0 THEN 1 ELSE 0 END
                ), 0) AS failures,
                COALESCE(SUM(translation_attempts.input_tokens), 0)
                    AS input_tokens,
                COALESCE(SUM(translation_attempts.output_tokens), 0)
                    AS output_tokens,
                COALESCE(SUM(translation_attempts.estimated_cost), 0)
                    AS estimated_cost
            FROM translation_attempts
        """
        parameters: tuple[str, ...] = ()
        if business_date is not None:
            query += """
                JOIN segments ON segments.id = translation_attempts.segment_id
                JOIN sessions ON sessions.id = segments.session_id
                WHERE sessions.business_date = ?
            """
            parameters = (validate_business_date(business_date),)
        row = self._connection.execute(query, parameters).fetchone()
        return TranslationUsageSummary(
            requests=int(row["requests"]),
            successes=int(row["successes"]),
            failures=int(row["failures"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            estimated_cost=float(row["estimated_cost"]),
        )

    def delete_day(self, business_date: str | date) -> DayDeletionResult:
        resolved_date = validate_business_date(business_date)
        with self.transaction() as connection:
            session_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE business_date = ?",
                    (resolved_date,),
                ).fetchone()[0]
            )
            segment_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM segments
                    JOIN sessions ON sessions.id = segments.session_id
                    WHERE sessions.business_date = ?
                    """,
                    (resolved_date,),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM sessions WHERE business_date = ?",
                (resolved_date,),
            )
        return DayDeletionResult(
            business_date=resolved_date,
            deleted_sessions=session_count,
            deleted_segments=segment_count,
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=int(row["id"]),
            business_date=str(row["business_date"]),
            name=str(row["name"]),
            started_at_utc=str(row["started_at_utc"]),
            ended_at_utc=(
                str(row["ended_at_utc"]) if row["ended_at_utc"] is not None else None
            ),
        )

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> SegmentRecord:
        return SegmentRecord(
            id=int(row["id"]),
            session_id=int(row["session_id"]),
            source_ja=str(row["source_ja"]),
            translation_zh=(
                str(row["translation_zh"])
                if row["translation_zh"] is not None
                else None
            ),
            finalized_at_utc=str(row["finalized_at_utc"]),
            translation_status=row["translation_status"],
            retry_count=int(row["retry_count"]),
            last_error=(
                str(row["last_error"]) if row["last_error"] is not None else None
            ),
            next_retry_at_utc=(
                str(row["next_retry_at_utc"])
                if row["next_retry_at_utc"] is not None
                else None
            ),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
        )

    @staticmethod
    def _insert_translation_attempt(
        connection: sqlite3.Connection,
        *,
        segment_id: int,
        attempted_at_utc: str,
        success: bool,
        error_code: str | None,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO translation_attempts(
                segment_id,
                attempted_at_utc,
                success,
                error_code,
                input_tokens,
                output_tokens,
                estimated_cost
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                attempted_at_utc,
                int(success),
                error_code,
                input_tokens,
                output_tokens,
                estimated_cost,
            ),
        )
