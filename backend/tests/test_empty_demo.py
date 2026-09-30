"""Offline checks use only temporary SQLite files and freshly invented records."""
import asyncio
import io
import os
from pathlib import Path
import secrets
import socket
import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select, func, text


def test_empty_offline_workflow(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("A local empty-data test must not open an outbound connection")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setenv("EXPENSE_FEISHU_APP_ID", "synthetic-forbidden-env")
    monkeypatch.setenv("EXPENSE_DATABASE_URL", "postgresql://forbidden.invalid/data")
    from app import config
    assert config.settings.FEISHU_APP_ID == ""
    assert config.settings.DATABASE_URL.startswith("sqlite+aiosqlite:///")
    with tempfile.TemporaryDirectory(prefix="expense-empty-test-") as tmp:
        temp_dir = Path(tmp)
        monkeypatch.setattr(config, "RUNTIME_DIR", temp_dir)
        object.__setattr__(config.settings, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_dir / 'test.sqlite3'}")
        object.__setattr__(config.settings, "VOUCHER_FILE_ROOT", str(temp_dir / "attachments"))
        from app.main import app
        from app.database import async_session, Base
        from app.models import User, Approval
        from app.auth import hash_password
        from app.services.voucher_feishu_import import download_feishu_attachment, FeishuImportError
        from app.services.voucher_feishu_comment import create_feishu_approval_comment, FeishuCommentSyncError
        from app.services.feishu_sync import run_sync

        async def counts():
            async with async_session() as db:
                return {table: await db.scalar(text(f'SELECT count(*) FROM "{table}"'))
                        for table in Base.metadata.tables}

        password = secrets.token_urlsafe(20)

        async def add_synthetic_admin():
            async with async_session() as db:
                db.add(User(username="demo-tester", display_name="虚构演示用户", role="admin",
                            password_hash=hash_password(password)))
                await db.commit()

        with TestClient(app) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json()["external_sync"] is False
            initial = asyncio.run(counts())
            assert initial["expense_categories"] == 4
            assert all(count == 0 for name, count in initial.items() if name != "expense_categories")
            assert client.post("/api/v1/sync/trigger").status_code == 403
            assert client.post("/api/v1/vouchers/items/1/import-feishu-attachments").status_code == 403
            assert client.post("/api/v1/vouchers/items/1/receipt-invoices/sync-feishu-comments").status_code == 403
            with pytest.raises(FeishuImportError):
                asyncio.run(download_feishu_attachment("https://forbidden.invalid/file", 100))
            with pytest.raises(FeishuImportError):
                asyncio.run(run_sync())
            with pytest.raises(FeishuCommentSyncError):
                asyncio.run(create_feishu_approval_comment("synthetic", instance_code="demo", actor_open_id="demo", filename="demo.pdf", size_bytes=1, attachment_url="https://forbidden.invalid/file", event_key="demo"))

            asyncio.run(add_synthetic_admin())
            login = client.post("/api/v1/auth/login", json={"username": "demo-tester", "password": password})
            assert login.status_code == 200
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            approvals = client.get("/api/v1/approvals").json()
            assert approvals["total"] == 0 and approvals["items"] == []
            assert client.get("/api/v1/vouchers/incomplete").json()["total"] == 0
            dashboard = client.get("/api/v1/dashboard/summary", params={"year_month": "2030-01"})
            assert dashboard.status_code == 200
            batch = client.post("/api/v1/vouchers/batches", json={"year_month": "2030-01", "sync_feishu": False})
            assert batch.status_code == 200
            batch_id = batch.json()["id"]
            assert batch.json()["summary"]["total"] == 0

            # A blank synthetic PDF proves local receipt storage/export still works.
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=200)
            pdf = io.BytesIO()
            writer.write(pdf)
            uploaded = client.post(f"/api/v1/vouchers/batches/{batch_id}/receipts", files={"files": ("synthetic-empty-receipt.pdf", pdf.getvalue(), "application/pdf")})
            assert uploaded.status_code == 200, uploaded.text
            assert uploaded.json()["uploaded"] == 1
            pool = client.get(f"/api/v1/vouchers/batches/{batch_id}/receipts")
            assert pool.status_code == 200
            rows = pool.json()
            assert len(rows) == 1
            download = client.get(f"/api/v1/vouchers/documents/{rows[0]['id']}/download")
            assert download.status_code == 200
            assert len(PdfReader(io.BytesIO(download.content)).pages) == 1
            packet = client.post(f"/api/v1/vouchers/batches/{batch_id}/receipts/export", json={"document_ids": [rows[0]["id"]]})
            assert packet.status_code == 200, packet.text
            assert len(PdfReader(io.BytesIO(packet.content)).pages) == 1

            async def add_synthetic_approval():
                async with async_session() as db:
                    db.add(Approval(instance_code="synthetic-demo-approval", definition_code="synthetic-definition",
                                    definition_name="虚构演示报销", applicant_name="虚构演示用户", status=2,
                                    amount=12.34, amount_cny=12.34, currency="CNY", submit_date=date(2030, 1, 2),
                                    description="仅用于自动测试的虚构资料", raw_summaries="[]"))
                    await db.commit()
            asyncio.run(add_synthetic_approval())
            rebuilt = client.post("/api/v1/vouchers/batches", json={"year_month": "2030-01", "sync_feishu": False})
            assert rebuilt.status_code == 200
            listed = client.get(f"/api/v1/vouchers/batches/{batch_id}/items").json()
            item_id = listed["items"][0]["id"]
            invoice = client.post(f"/api/v1/vouchers/items/{item_id}/invoice", files={"file": ("synthetic-invoice.pdf", pdf.getvalue(), "application/pdf")})
            assert invoice.status_code == 200, invoice.text
            item_pdf = client.get(f"/api/v1/vouchers/items/{item_id}/pdf")
            assert item_pdf.status_code == 200, item_pdf.text
            assert len(PdfReader(io.BytesIO(item_pdf.content)).pages) >= 2


def test_source_has_no_network_clients_or_plaintext_password_storage():
    root = Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        source = path.read_text()
        assert "httpx.AsyncClient(" not in source, str(path)
        assert "socket.getaddrinfo(" not in source, str(path)
        assert "display_password" not in source, str(path)
    assert "create_task(" not in (root / "main.py").read_text()
