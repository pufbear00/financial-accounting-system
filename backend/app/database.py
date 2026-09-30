from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings, RUNTIME_DIR

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# SQLite 的 create_all 只会创建新表，不会给已有表补充后来新增的列。
# 这里集中维护幂等兼容迁移，避免旧数据卷在升级后因查询新列而触发 500。
SCHEMA_COMPAT_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "airwallex_transactions": (
        ("exchange_rate", "FLOAT"),
        ("amount_cny", "FLOAT"),
    ),
    "approvals": (
        ("exchange_rate", "FLOAT"),
        ("amount_cny", "FLOAT"),
        ("submit_date", "DATE"),
    ),
    "voucher_items": (
        ("source_line_key", "VARCHAR(80) NOT NULL DEFAULT 'main'"),
        ("source_line_label", "VARCHAR(120)"),
        ("requires_voucher", "BOOLEAN NOT NULL DEFAULT 1"),
        ("voucher_exclusion_reason", "VARCHAR"),
        ("requirement_overridden", "BOOLEAN NOT NULL DEFAULT 0"),
        ("include_approval_in_monthly_pdf", "BOOLEAN NOT NULL DEFAULT 0"),
    ),
    "voucher_documents": (
        ("receipt_direction", "VARCHAR(20)"),
        ("receipt_category", "VARCHAR(20)"),
    ),
    "voucher_document_links": (
        ("invoice_printed", "BOOLEAN NOT NULL DEFAULT 0"),
    ),
    "voucher_receipt_invoice_links": (
        ("owns_invoice", "BOOLEAN NOT NULL DEFAULT 1"),
    ),
}


async def _table_exists(conn, table: str) -> bool:
    result = await conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    return result.first() is not None


async def _has_unique_index(conn, table: str, columns: tuple[str, ...]) -> bool:
    if not await _table_exists(conn, table):
        return False
    result = await conn.exec_driver_sql(f'PRAGMA index_list("{table}")')
    for row in result.fetchall():
        # row: seq, name, unique, origin, partial
        if not row[2]:
            continue
        index_name = str(row[1]).replace('"', '""')
        info = await conn.exec_driver_sql(f'PRAGMA index_info("{index_name}")')
        if tuple(col[2] for col in info.fetchall()) == columns:
            return True
    return False


async def _restore_split_line_snapshots(conn) -> bool:
    """Restore aggregate approval data when a split-line release is detected."""
    if not await _table_exists(conn, "voucher_items") or not await _table_exists(
        conn, "approvals"
    ):
        return False
    affected = await conn.exec_driver_sql(
        """
        SELECT DISTINCT batch_id, approval_id
        FROM voucher_items
        WHERE COALESCE(source_line_key, 'main') != 'main'
           OR source_line_label IS NOT NULL
        """
    )
    affected_pairs = [
        (int(batch_id), int(approval_id))
        for batch_id, approval_id in affected.fetchall()
    ]
    if not affected_pairs:
        return False
    for batch_id, approval_id in affected_pairs:
        await conn.exec_driver_sql(
            """
            UPDATE voucher_items
            SET source_line_key = 'main',
                source_line_label = NULL,
                instance_code = (
                    SELECT approvals.instance_code FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                definition_name = (
                    SELECT approvals.definition_name FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                applicant_name = (
                    SELECT approvals.applicant_name FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                approval_status = (
                    SELECT approvals.status FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                amount = (
                    SELECT approvals.amount FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                currency = (
                    SELECT approvals.currency FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                amount_cny = (
                    SELECT approvals.amount_cny FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                category_id = (
                    SELECT approvals.category_id FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                expense_date = (
                    SELECT approvals.expense_date FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                submit_date = (
                    SELECT approvals.submit_date FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                description = (
                    SELECT approvals.description FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                ),
                raw_summaries_snapshot = (
                    SELECT approvals.raw_summaries FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                )
            WHERE batch_id = ?
              AND approval_id = ?
              AND EXISTS (
                    SELECT 1 FROM approvals
                    WHERE approvals.id = voucher_items.approval_id
                  )
            """,
            (batch_id, approval_id),
        )
    return True


async def _merge_duplicate_voucher_items(conn) -> set[int]:
    """Merge legacy split-payment rows into one item per approval.

    The earliest sequence/id is retained. Distinct attachments and export history
    are moved to that row before duplicate rows are removed.
    """
    if not await _table_exists(conn, "voucher_items"):
        return set()
    groups = await conn.exec_driver_sql(
        """
        SELECT batch_id, approval_id
        FROM voucher_items
        GROUP BY batch_id, approval_id
        HAVING COUNT(*) > 1
        """
    )
    changed_batches: set[int] = set()
    has_links = await _table_exists(conn, "voucher_document_links")
    has_exports = await _table_exists(conn, "voucher_exports")
    for batch_id, approval_id in groups.fetchall():
        rows = await conn.exec_driver_sql(
            """
            SELECT id, sequence
            FROM voucher_items
            WHERE batch_id = ? AND approval_id = ?
            ORDER BY sequence, id
            """,
            (batch_id, approval_id),
        )
        item_ids = [int(row[0]) for row in rows.fetchall()]
        if len(item_ids) < 2:
            continue
        survivor_id, *duplicate_ids = item_ids
        changed_batches.add(int(batch_id))
        for duplicate_id in duplicate_ids:
            if has_links:
                # Remove only links already present on the survivor; all other
                # invoice and receipt links are transferred intact.
                await conn.exec_driver_sql(
                    """
                    DELETE FROM voucher_document_links
                    WHERE item_id = ?
                      AND EXISTS (
                          SELECT 1
                          FROM voucher_document_links survivor_link
                          WHERE survivor_link.item_id = ?
                            AND survivor_link.document_id = voucher_document_links.document_id
                      )
                    """,
                    (duplicate_id, survivor_id),
                )
                await conn.exec_driver_sql(
                    "UPDATE voucher_document_links SET item_id = ? WHERE item_id = ?",
                    (survivor_id, duplicate_id),
                )
            if has_exports:
                await conn.exec_driver_sql(
                    "UPDATE voucher_exports SET item_id = ? WHERE item_id = ?",
                    (survivor_id, duplicate_id),
                )
            await conn.exec_driver_sql(
                "DELETE FROM voucher_items WHERE id = ?", (duplicate_id,)
            )
        # The retained row represents the complete approval, not a parsed form
        # line. Existing aggregate snapshots are preserved.
        await conn.exec_driver_sql(
            """
            UPDATE voucher_items
            SET source_line_key = 'main', source_line_label = NULL
            WHERE id = ?
            """,
            (survivor_id,),
        )
    return changed_batches


async def _normalize_voucher_sequences(conn) -> bool:
    """Make voucher numbers contiguous without colliding with an old unique index."""
    if not await _table_exists(conn, "voucher_items"):
        return False
    result = await conn.exec_driver_sql(
        "SELECT DISTINCT batch_id FROM voucher_items ORDER BY batch_id"
    )
    changed = False
    for (batch_id,) in result.fetchall():
        rows = await conn.exec_driver_sql(
            """
            SELECT id, sequence
            FROM voucher_items
            WHERE batch_id = ?
            ORDER BY sequence, id
            """,
            (batch_id,),
        )
        ordered = [(int(row[0]), int(row[1])) for row in rows.fetchall()]
        if all(old_sequence == index for index, (_, old_sequence) in enumerate(ordered, 1)):
            continue
        changed = True
        old_sequences = [row[1] for row in ordered]
        minimum = min(old_sequences)
        maximum = max(old_sequences)
        offset = maximum - minimum + len(ordered) + 100
        # First move the whole batch beyond its current sequence range. This is
        # safe even if a legacy batch+sequence unique index already exists.
        await conn.exec_driver_sql(
            "UPDATE voucher_items SET sequence = sequence + ? WHERE batch_id = ?",
            (offset, batch_id),
        )
        for sequence, (item_id, _old_sequence) in enumerate(ordered, 1):
            await conn.exec_driver_sql(
                "UPDATE voucher_items SET sequence = ? WHERE id = ?",
                (sequence, item_id),
            )
    return changed


async def _refresh_voucher_batch_counts(conn) -> bool:
    if not await _table_exists(conn, "voucher_batches"):
        return False
    result = await conn.exec_driver_sql(
        """
        UPDATE voucher_batches
        SET item_count = (
            SELECT COUNT(*) FROM voucher_items
            WHERE voucher_items.batch_id = voucher_batches.id
        )
        WHERE item_count != (
            SELECT COUNT(*) FROM voucher_items
            WHERE voucher_items.batch_id = voucher_batches.id
        )
        """
    )
    return bool(result.rowcount)


async def _backfill_requirement_overrides(conn) -> bool:
    if not await _table_exists(conn, "voucher_items"):
        return False
    result = await conn.exec_driver_sql(
        """
        UPDATE voucher_items
        SET requirement_overridden = 1
        WHERE COALESCE(requirement_overridden, 0) = 0
          AND voucher_exclusion_reason LIKE '人工%'
        """
    )
    return bool(result.rowcount)


async def _ensure_voucher_item_unique_indexes(conn) -> list[str]:
    if not await _table_exists(conn, "voucher_items"):
        return []
    applied: list[str] = []
    if not await _has_unique_index(
        conn, "voucher_items", ("batch_id", "approval_id")
    ):
        await conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_voucher_item_batch_approval
            ON voucher_items (batch_id, approval_id)
            """
        )
        applied.append("unique_batch_approval")
    if not await _has_unique_index(
        conn, "voucher_items", ("batch_id", "sequence")
    ):
        await conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_voucher_item_batch_sequence
            ON voucher_items (batch_id, sequence)
            """
        )
        applied.append("unique_batch_sequence")
    return applied


async def ensure_schema_compatibility(conn) -> list[tuple[str, str]]:
    """Add missing columns to legacy SQLite tables and return applied changes."""
    applied: list[tuple[str, str]] = []
    for table, columns in SCHEMA_COMPAT_COLUMNS.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        rows = result.fetchall()
        if not rows:
            continue
        existing = {row[1] for row in rows}
        for column, column_type in columns:
            if column in existing:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            )
            applied.append((table, column))
    if await _restore_split_line_snapshots(conn):
        applied.append(("voucher_items", "restore_approval_snapshots"))
    duplicate_batches = await _merge_duplicate_voucher_items(conn)
    if duplicate_batches:
        applied.append(("voucher_items", "merge_duplicate_batch_approval_items"))
    if await _normalize_voucher_sequences(conn):
        applied.append(("voucher_items", "normalize_batch_sequences"))
    if await _refresh_voucher_batch_counts(conn):
        applied.append(("voucher_batches", "refresh_item_count"))
    if await _backfill_requirement_overrides(conn):
        applied.append(("voucher_items", "backfill_requirement_overrides"))
    for index_name in await _ensure_voucher_item_unique_indexes(conn):
        applied.append(("voucher_items", index_name))
    receipt_indexes = await conn.exec_driver_sql(
        "PRAGMA index_list(voucher_document_links)"
    )
    if any(row[1] == "uq_voucher_bank_receipt_per_item" for row in receipt_indexes.fetchall()):
        await conn.exec_driver_sql(
            "DROP INDEX IF EXISTS uq_voucher_bank_receipt_per_item"
        )
        applied.append(
            ("voucher_document_links", "allow_multiple_receipts_per_item")
        )
    return applied


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session
