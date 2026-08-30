from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .api import ConnectorConflictError, ConnectorError


SCHEMA_VERSION = "4"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OrderRecord:
    external_run_id: str
    request_digest: str
    run_id: str | None
    status: str
    status_version: int
    output_status: str | None
    result_digest: str | None
    result_manifest: dict[str, Any] | None
    export_request_digest: str | None
    export_request: dict[str, Any] | None
    export_id: str | None
    export_manifest_digest: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventResult:
    replayed: bool
    applied: bool
    stale: bool
    record: OrderRecord


class ConnectorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ConnectorError(
                "CONNECTOR_STATE_PATH_INVALID",
                "Connector 状态目录必须是真实目录。",
            )
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ConnectorError(
                "CONNECTOR_STATE_PATH_INVALID",
                "Connector SQLite 路径必须是普通文件。",
            )
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS connector_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    external_run_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    run_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    status_version INTEGER NOT NULL,
                    output_status TEXT,
                    result_digest TEXT,
                    result_manifest TEXT,
                    export_request_digest TEXT,
                    export_request TEXT,
                    export_id TEXT UNIQUE,
                    export_manifest_digest TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    external_run_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    status_version INTEGER NOT NULL,
                    applied INTEGER NOT NULL,
                    stale INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    FOREIGN KEY (external_run_id)
                        REFERENCES orders(external_run_id)
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    external_run_id TEXT NOT NULL,
                    output_key TEXT NOT NULL,
                    semantic_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (external_run_id, output_key),
                    FOREIGN KEY (external_run_id)
                        REFERENCES orders(external_run_id)
                );
                """
            )
            current = connection.execute(
                "SELECT value FROM connector_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO connector_metadata(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif current["value"] in {"1", "2", "3"}:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(orders)").fetchall()
                }
                if "result_manifest" not in columns:
                    connection.execute("ALTER TABLE orders ADD COLUMN result_manifest TEXT")
                if "export_request_digest" not in columns:
                    connection.execute(
                        "ALTER TABLE orders ADD COLUMN export_request_digest TEXT"
                    )
                if "export_request" not in columns:
                    connection.execute(
                        "ALTER TABLE orders ADD COLUMN export_request TEXT"
                    )
                connection.execute(
                    "UPDATE connector_metadata SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            elif current["value"] != SCHEMA_VERSION:
                raise ConnectorError(
                    "CONNECTOR_STATE_SCHEMA_UNSUPPORTED",
                    "Connector SQLite schema 版本不受支持。",
                )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _result_manifest(value: str | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            manifest = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ConnectorError(
                "CONNECTOR_STATE_CORRUPT",
                "Connector 中持久化的结果清单无效。",
            ) from error
        if not isinstance(manifest, dict):
            raise ConnectorError(
                "CONNECTOR_STATE_CORRUPT",
                "Connector 中持久化的结果清单必须是 object。",
            )
        return manifest

    @staticmethod
    def _record(row: sqlite3.Row | None) -> OrderRecord:
        if row is None:
            raise ConnectorError(
                "CONNECTOR_ORDER_NOT_FOUND",
                "Connector 中不存在该 external_run_id。",
            )
        return OrderRecord(
            external_run_id=row["external_run_id"],
            request_digest=row["request_digest"],
            run_id=row["run_id"],
            status=row["status"],
            status_version=int(row["status_version"]),
            output_status=row["output_status"],
            result_digest=row["result_digest"],
            result_manifest=ConnectorStore._result_manifest(row["result_manifest"]),
            export_request_digest=row["export_request_digest"],
            export_request=ConnectorStore._result_manifest(row["export_request"]),
            export_id=row["export_id"],
            export_manifest_digest=row["export_manifest_digest"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _select_order(
        connection: sqlite3.Connection, external_run_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM orders WHERE external_run_id = ?",
            (external_run_id,),
        ).fetchone()

    def register_order(
        self, external_run_id: str, request_digest: str
    ) -> tuple[OrderRecord, bool]:
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, external_run_id)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO orders(
                        external_run_id, request_digest, status, status_version,
                        created_at, updated_at
                    ) VALUES (?, ?, 'submitting', 0, ?, ?)
                    """,
                    (external_run_id, request_digest, now, now),
                )
                row = self._select_order(connection, external_run_id)
                return self._record(row), True
            record = self._record(row)
            if record.request_digest != request_digest:
                raise ConnectorConflictError(
                    "相同 MES external_run_id 已映射到不同 Analysis Request。",
                    details={"external_run_id": external_run_id},
                )
            return record, False

    def get_order(self, external_run_id: str) -> OrderRecord:
        with self._connect() as connection:
            return self._record(self._select_order(connection, external_run_id))

    def bind_run(
        self,
        external_run_id: str,
        run_id: str,
        *,
        status: str,
        status_version: int,
        output_status: str,
    ) -> OrderRecord:
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.run_id is not None and record.run_id != run_id:
                raise ConnectorConflictError(
                    "相同 MES external_run_id 已绑定其他 AnalysisRun。",
                    details={
                        "external_run_id": external_run_id,
                        "existing_run_id": record.run_id,
                        "received_run_id": run_id,
                    },
                )
            version = int(status_version)
            if version == record.status_version:
                if record.status not in {"submitting", status}:
                    raise ConnectorConflictError(
                        "相同 status_version 对应了不同运行状态。"
                    )
            next_version = max(record.status_version, version)
            next_status = status if version >= record.status_version else record.status
            next_output_status = (
                output_status if version >= record.status_version else record.output_status
            )
            connection.execute(
                """
                UPDATE orders
                SET run_id = ?, status = ?, status_version = ?,
                    output_status = ?, updated_at = ?
                WHERE external_run_id = ?
                """,
                (
                    run_id,
                    next_status,
                    next_version,
                    next_output_status,
                    _now(),
                    external_run_id,
                ),
            )
            return self._record(self._select_order(connection, external_run_id))

    def update_from_poll(
        self,
        external_run_id: str,
        *,
        run_id: str,
        status: str,
        status_version: int,
        output_status: str | None,
    ) -> OrderRecord:
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.run_id not in {None, run_id}:
                raise ConnectorConflictError("轮询结果的 run_id 与 Connector 绑定不一致。")
            version = int(status_version)
            if version < record.status_version:
                return record
            if (
                version == record.status_version
                and record.status not in {"submitting", status}
            ):
                raise ConnectorConflictError("相同 status_version 对应了不同运行状态。")
            connection.execute(
                """
                UPDATE orders
                SET run_id = ?, status = ?, status_version = ?,
                    output_status = ?, updated_at = ?
                WHERE external_run_id = ?
                """,
                (run_id, status, version, output_status, _now(), external_run_id),
            )
            return self._record(self._select_order(connection, external_run_id))

    def _existing_event(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        payload_digest: str,
    ) -> sqlite3.Row | None:
        event = connection.execute(
            "SELECT * FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if event is not None and event["payload_digest"] != payload_digest:
            raise ConnectorConflictError("相同 Webhook event_id 收到了不同 payload。")
        return event

    def find_event_replay(
        self,
        *,
        event_id: str,
        payload_digest: str,
        external_run_id: str,
    ) -> EventResult | None:
        with self._transaction() as connection:
            existing = self._existing_event(connection, event_id, payload_digest)
            if existing is None:
                return None
            if existing["external_run_id"] != external_run_id:
                raise ConnectorConflictError(
                    "Webhook event_id 与已记录的 MES 任务不一致。"
                )
            return EventResult(
                True,
                False,
                bool(existing["stale"]),
                self._record(self._select_order(connection, external_run_id)),
            )

    def apply_terminal_event(
        self,
        *,
        event_id: str,
        delivery_id: str,
        external_run_id: str,
        run_id: str,
        status: str,
        status_version: int,
        output_status: str,
        payload_digest: str,
    ) -> EventResult:
        with self._transaction() as connection:
            existing = self._existing_event(connection, event_id, payload_digest)
            record = self._record(self._select_order(connection, external_run_id))
            if existing is not None:
                return EventResult(True, False, bool(existing["stale"]), record)
            if record.run_id not in {None, run_id}:
                raise ConnectorConflictError("Webhook run_id 与 Connector 绑定不一致。")
            version = int(status_version)
            stale = version < record.status_version
            applied = not stale
            if (
                version == record.status_version
                and record.status not in {"submitting", status}
            ):
                raise ConnectorConflictError("相同 status_version 对应了冲突的终态事件。")
            if (
                version == record.status_version
                and record.output_status not in {None, output_status}
            ):
                raise ConnectorConflictError("相同 status_version 对应了冲突的输出状态。")
            if applied:
                connection.execute(
                    """
                    UPDATE orders
                    SET run_id = ?, status = ?, status_version = ?,
                        output_status = ?, updated_at = ?
                    WHERE external_run_id = ?
                    """,
                    (
                        run_id,
                        status,
                        version,
                        output_status,
                        _now(),
                        external_run_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO webhook_events(
                    event_id, delivery_id, event_type, external_run_id,
                    payload_digest, status_version, applied, stale, received_at
                ) VALUES (?, ?, 'analysis.run.terminal', ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    delivery_id,
                    external_run_id,
                    payload_digest,
                    version,
                    int(applied),
                    int(stale),
                    _now(),
                ),
            )
            return EventResult(
                False,
                applied,
                stale,
                self._record(self._select_order(connection, external_run_id)),
            )

    def apply_export_event(
        self,
        *,
        event_id: str,
        delivery_id: str,
        external_run_id: str,
        run_id: str,
        status_version: int,
        export_id: str,
        manifest_digest: str,
        payload_digest: str,
    ) -> EventResult:
        with self._transaction() as connection:
            existing = self._existing_event(connection, event_id, payload_digest)
            record = self._record(self._select_order(connection, external_run_id))
            if existing is not None:
                return EventResult(True, False, False, record)
            if record.export_request_digest is None or record.export_request is None:
                raise ConnectorConflictError(
                    "尚未固定 Artifact Export 请求，拒绝绑定完成事件。"
                )
            if record.run_id not in {None, run_id}:
                raise ConnectorConflictError("Artifact Webhook run_id 与 Connector 绑定不一致。")
            if record.export_id not in {None, export_id}:
                raise ConnectorConflictError("MES 任务已绑定其他 Artifact Export。")
            if record.export_manifest_digest not in {None, manifest_digest}:
                raise ConnectorConflictError("Artifact Export 清单摘要发生冲突。")
            connection.execute(
                """
                UPDATE orders
                SET run_id = ?, export_id = ?, export_manifest_digest = ?,
                    updated_at = ?
                WHERE external_run_id = ?
                """,
                (run_id, export_id, manifest_digest, _now(), external_run_id),
            )
            connection.execute(
                """
                INSERT INTO webhook_events(
                    event_id, delivery_id, event_type, external_run_id,
                    payload_digest, status_version, applied, stale, received_at
                ) VALUES (?, ?, 'analysis.artifact_export.completed', ?, ?, ?, 1, 0, ?)
                """,
                (
                    event_id,
                    delivery_id,
                    external_run_id,
                    payload_digest,
                    int(status_version),
                    _now(),
                ),
            )
            return EventResult(
                False,
                True,
                False,
                self._record(self._select_order(connection, external_run_id)),
            )

    def set_export(self, external_run_id: str, export_id: str) -> OrderRecord:
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.export_id not in {None, export_id}:
                raise ConnectorConflictError("MES 任务已绑定其他 Artifact Export。")
            connection.execute(
                "UPDATE orders SET export_id = ?, updated_at = ? WHERE external_run_id = ?",
                (export_id, _now(), external_run_id),
            )
            return self._record(self._select_order(connection, external_run_id))

    def claim_export_request(
        self,
        external_run_id: str,
        request_digest: str,
        request: dict[str, Any],
    ) -> OrderRecord:
        try:
            serialized = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ConnectorError(
                "CONNECTOR_EXPORT_REQUEST_INVALID",
                "Artifact Export 请求不能安全序列化。",
            ) from error
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.export_request_digest not in {None, request_digest}:
                raise ConnectorConflictError(
                    "相同 MES 任务已固定其他 Artifact Export 请求。"
                )
            if record.export_request is not None and record.export_request != request:
                raise ConnectorConflictError(
                    "相同 MES 任务的 Artifact Export 请求内容发生变化。"
                )
            connection.execute(
                """
                UPDATE orders
                SET export_request_digest = ?, export_request = ?, updated_at = ?
                WHERE external_run_id = ?
                """,
                (request_digest, serialized, _now(), external_run_id),
            )
            return self._record(self._select_order(connection, external_run_id))

    def set_export_manifest(
        self,
        external_run_id: str,
        *,
        export_id: str,
        manifest_digest: str,
    ) -> OrderRecord:
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.export_id != export_id:
                raise ConnectorConflictError("Artifact Export ID 与 MES 任务绑定不一致。")
            if record.export_manifest_digest not in {None, manifest_digest}:
                raise ConnectorConflictError("Artifact Export 清单摘要发生冲突。")
            connection.execute(
                """
                UPDATE orders SET export_manifest_digest = ?, updated_at = ?
                WHERE external_run_id = ?
                """,
                (manifest_digest, _now(), external_run_id),
            )
            return self._record(self._select_order(connection, external_run_id))

    def commit_result(
        self,
        external_run_id: str,
        result_digest: str,
        result_manifest: dict[str, Any],
        outputs: list[dict[str, Any]],
    ) -> OrderRecord:
        try:
            serialized = json.dumps(
                result_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ConnectorError(
                "CONNECTOR_RESULT_MANIFEST_INVALID",
                "Connector 结果清单不能安全序列化。",
            ) from error
        with self._transaction() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.result_digest not in {None, result_digest}:
                raise ConnectorConflictError("相同 MES 任务的结果清单摘要发生变化。")
            if (
                record.result_manifest is not None
                and record.result_manifest != result_manifest
            ):
                raise ConnectorConflictError("相同 MES 任务的结果清单内容发生变化。")
            expected_outputs: dict[str, tuple[str, int, str, str]] = {}
            for output in outputs:
                key = str(output["output_key"])
                values = (
                    str(output["semantic_type"]),
                    int(output["size"]),
                    str(output["sha256"]),
                    str(output["local_path"]),
                )
                if key in expected_outputs:
                    raise ConnectorConflictError("结果回执包含重复 output key。")
                expected_outputs[key] = values
            existing_rows = connection.execute(
                "SELECT * FROM outputs WHERE external_run_id = ?",
                (external_run_id,),
            ).fetchall()
            existing_outputs = {
                str(row["output_key"]): (
                    str(row["semantic_type"]),
                    int(row["size"]),
                    str(row["sha256"]),
                    str(row["local_path"]),
                )
                for row in existing_rows
            }
            if set(existing_outputs) - set(expected_outputs):
                raise ConnectorConflictError("已持久化输出不属于当前结果清单。")
            for key, values in expected_outputs.items():
                if key in existing_outputs:
                    if existing_outputs[key] != values:
                        raise ConnectorConflictError("相同输出 key 的结果回执发生冲突。")
                    continue
                connection.execute(
                    """
                    INSERT INTO outputs(
                        external_run_id, output_key, semantic_type, size,
                        sha256, local_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (external_run_id, key, *values, _now()),
                )
            connection.execute(
                """
                UPDATE orders
                SET result_digest = ?, result_manifest = ?, updated_at = ?
                WHERE external_run_id = ?
                """,
                (result_digest, serialized, _now(), external_run_id),
            )
            return self._record(self._select_order(connection, external_run_id))

    def list_outputs(self, external_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.result_digest is None or record.result_manifest is None:
                return []
            rows = connection.execute(
                """
                SELECT output_key, semantic_type, size, sha256, local_path
                FROM outputs WHERE external_run_id = ? ORDER BY output_key
                """,
                (external_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_output(self, external_run_id: str, output_key: str) -> dict[str, Any]:
        with self._connect() as connection:
            record = self._record(self._select_order(connection, external_run_id))
            if record.result_digest is None or record.result_manifest is None:
                raise ConnectorError(
                    "CONNECTOR_OUTPUT_NOT_FOUND",
                    "结果清单尚未完整提交，输出不可交付。",
                )
            row = connection.execute(
                """
                SELECT output_key, semantic_type, size, sha256, local_path
                FROM outputs
                WHERE external_run_id = ? AND output_key = ?
                """,
                (external_run_id, output_key),
            ).fetchone()
        if row is None:
            raise ConnectorError(
                "CONNECTOR_OUTPUT_NOT_FOUND",
                "Connector 中不存在该已验证输出。",
            )
        receipt = dict(row)
        results = record.result_manifest.get("results")
        matching = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("kind") == "file"
            and item.get("key") == output_key
            and item.get("size") == receipt["size"]
            and item.get("sha256") == receipt["sha256"]
        ] if isinstance(results, list) else []
        if len(matching) != 1:
            raise ConnectorError(
                "CONNECTOR_OUTPUT_NOT_FOUND",
                "输出回执不属于已提交结果清单。",
            )
        return receipt

    def event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM webhook_events").fetchone()
            return int(row["count"])
