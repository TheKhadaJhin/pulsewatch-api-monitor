from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from app import cli
from app.config import Settings
from app.logging_config import JsonFormatter, configure_logging
from app.models import CheckResultOut


def fake_components(settings: Settings) -> tuple[SimpleNamespace, ...]:
    database = SimpleNamespace(
        initialize=AsyncMock(),
        dispose=AsyncMock(),
        ping=AsyncMock(return_value=True),
        schema_ready=AsyncMock(return_value=True),
    )
    repository = SimpleNamespace(
        create_service=AsyncMock(),
        build_summary=AsyncMock(),
        purge_history=AsyncMock(return_value=4),
    )
    monitor = SimpleNamespace(run_checks=AsyncMock(return_value=[]))
    scheduler = SimpleNamespace(
        run_once=AsyncMock(return_value=2),
        start=AsyncMock(),
        stop=AsyncMock(),
    )
    return settings, database, repository, monitor, scheduler


@pytest.mark.asyncio
async def test_build_components_wires_runtime_dependencies(tmp_path: Path) -> None:
    settings = Settings.for_sqlite(tmp_path / "components.db")
    runtime, database, repository, monitor, scheduler = cli.build_components(settings)
    assert runtime is settings
    assert repository.database is database
    assert monitor.repository is repository
    assert scheduler.monitor is monitor
    await database.dispose()


@pytest.mark.asyncio
async def test_init_seed_summary_scheduler_and_retention_commands(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    components = fake_components(settings)
    _, database, repository, _, scheduler = components
    repository.create_service.side_effect = [
        None,
        IntegrityError("duplicate", {}, RuntimeError("unique")),
    ]
    summary = Mock()
    summary.model_dump_json.return_value = '{"services_total": 2}'
    repository.build_summary.return_value = summary
    monkeypatch.setattr(cli, "build_components", lambda *_: components)

    await cli.init_database()
    await cli.seed_database()
    await cli.print_summary(48)
    await cli.run_scheduler_once()
    await cli.run_retention()

    output = capsys.readouterr().out
    assert "Database schema is ready" in output
    assert "Added: GitHub API" in output
    assert "Already exists: JSONPlaceholder" in output
    assert "Seed complete: 1 service(s) added" in output
    assert '"services_total": 2' in output
    assert "2 check(s) executed" in output
    assert "4 check result(s) removed" in output
    assert database.initialize.await_count == 5
    assert database.dispose.await_count == 5
    repository.build_summary.assert_awaited_once_with(
        window_hours=48,
        degraded_latency_ms=settings.degraded_latency_ms,
    )
    scheduler.run_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_command_handles_empty_and_mixed_results(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    components = fake_components(settings)
    monitor = components[3]
    monkeypatch.setattr(cli, "build_components", lambda *_: components)

    await cli.run_all_checks()
    assert "No active services" in capsys.readouterr().out

    now = datetime.now(UTC)
    monitor.run_checks.return_value = [
        CheckResultOut(
            id=1,
            service_id=1,
            checked_at=now,
            status_code=200,
            latency_ms=12.34,
            is_healthy=True,
            attempts=1,
            trigger="manual",
            error_type=None,
            error_message=None,
            request_id="cli",
        ),
        CheckResultOut(
            id=2,
            service_id=2,
            checked_at=now,
            status_code=None,
            latency_ms=None,
            is_healthy=False,
            attempts=3,
            trigger="manual",
            error_type="Timeout",
            error_message="timed out",
            request_id="cli",
        ),
    ]
    await cli.run_all_checks()
    output = capsys.readouterr().out
    assert "[OK] service=1 status=200 latency=12.34 ms attempts=1" in output
    assert "[FAIL] service=2 status=None latency=n/a attempts=3" in output


@pytest.mark.asyncio
async def test_worker_starts_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configured = Settings.for_sqlite(
        Path(settings.database_url.removeprefix("sqlite+aiosqlite:///")),
        auto_create_schema=True,
    )
    components = fake_components(configured)
    database = components[1]
    scheduler = components[4]

    class ImmediateEvent:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    class FakeLoop:
        def add_signal_handler(self, *_: object) -> None:
            return None

    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=configured))
    monkeypatch.setattr(cli, "build_components", lambda *_: components)
    monkeypatch.setattr(cli.asyncio, "Event", ImmediateEvent)
    monkeypatch.setattr(cli.asyncio, "get_running_loop", lambda: FakeLoop())

    await cli.run_worker()
    assert "scheduler worker started" in capsys.readouterr().out
    database.initialize.assert_awaited_once()
    database.ping.assert_awaited_once()
    database.schema_ready.assert_awaited_once()
    scheduler.start.assert_awaited_once()
    scheduler.stop.assert_awaited_once()
    database.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_fails_when_database_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    components = fake_components(settings)
    components[1].ping.return_value = False
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(cli, "build_components", lambda *_: components)
    with pytest.raises(RuntimeError, match="readiness"):
        await cli.run_worker()


def test_parse_args_and_main_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pulsewatch", "summary", "--hours", "72"])
    parsed = cli.parse_args()
    assert parsed.command == "summary"
    assert parsed.hours == 72

    commands = {
        "init-db": ("init_database", None),
        "seed": ("seed_database", None),
        "check": ("run_all_checks", None),
        "summary": ("print_summary", 12),
        "scheduler-once": ("run_scheduler_once", None),
        "worker": ("run_worker", None),
        "retention": ("run_retention", None),
    }
    for command, (target_name, argument) in commands.items():
        job = object()
        target = Mock(return_value=job)
        runner = Mock()
        namespace = argparse.Namespace(command=command, hours=12)
        monkeypatch.setattr(cli, "parse_args", Mock(return_value=namespace))
        monkeypatch.setattr(cli, target_name, target)
        monkeypatch.setattr(cli.asyncio, "run", runner)
        cli.main()
        if argument is None:
            target.assert_called_once_with()
        else:
            target.assert_called_once_with(argument)
        runner.assert_called_once_with(job)

    monkeypatch.setattr(
        cli,
        "parse_args",
        Mock(return_value=argparse.Namespace(command="generate-key")),
    )
    monkeypatch.setattr(cli.secrets, "token_urlsafe", Mock(return_value="secure-key"))
    cli.main()
    assert capsys.readouterr().out.strip().endswith("secure-key")


def test_json_and_human_logging_formatters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="pulsewatch.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request complete",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-1"
    record.method = "GET"
    record.status = 200
    payload = json.loads(formatter.format(record))
    assert payload["logger"] == "pulsewatch.test"
    assert payload["request_id"] == "req-1"
    assert payload["status"] == 200
    assert payload["timestamp"].endswith("+00:00")

    try:
        raise ValueError("boom")
    except ValueError:
        error_record = logging.LogRecord(
            name="pulsewatch.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    assert "ValueError: boom" in json.loads(formatter.format(error_record))["exception"]

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        configure_logging(level="WARNING", use_json=True)
        logging.getLogger("pulsewatch.test").warning("json warning")
        assert '"message":"json warning"' in capsys.readouterr().err
        configure_logging(level="INFO", use_json=False)
        logging.getLogger("pulsewatch.test").info("human info")
        assert "pulsewatch.test | human info" in capsys.readouterr().err
    finally:
        root.handlers.clear()
        root.handlers.extend(previous_handlers)
        root.setLevel(previous_level)
