"""CLI rendering and the import command, without a live Docker daemon."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from tetherd.cli import app, rebuild_named, render_doctor, render_status
from tetherd.config import ProbeSettings, Settings
from tetherd.daemon import Runtime
from tetherd.docker_api import DockerApi
from tetherd.notify import Notifier
from tetherd.provider import ProviderMonitor
from tetherd.remediate import Remediator
from tetherd.snapshots import SnapshotStore
from tetherd.state import ProviderStateStore

from .conftest import make_inspect
from .fakes import FakeDocker

PROVIDER_ID = "a" * 64
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("TETHERD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("TETHERD_CONFIG_FILE", "/nonexistent/tetherd.yaml")


def runtime_for(docker: FakeDocker, tmp_path: Path, **overrides: Any) -> Runtime:
    settings = Settings(
        provider="gluetun",
        state_dir=tmp_path,
        probe=ProbeSettings(settle_seconds=0.0),
        **overrides,
    )
    api = cast(DockerApi, docker)
    snapshots = SnapshotStore(settings.snapshot_dir)
    remediator = Remediator(api, snapshots, sleep=lambda _: None, restart_grace_seconds=1.0)
    monitor = ProviderMonitor(api, settings.probe, sleep=lambda _: None)
    from tetherd.reconcile import Reconciler

    return Runtime(
        settings=settings,
        api=api,
        snapshots=snapshots,
        remediator=remediator,
        reconciler=Reconciler(
            api,
            settings,
            snapshots=snapshots,
            remediator=remediator,
            monitor=monitor,
            state=ProviderStateStore(settings.provider_state_file),
        ),
        monitor=monitor,
        notifier=Notifier([]),
        lock=None,  # type: ignore[arg-type]
        state=ProviderStateStore(settings.provider_state_file),
    )


class TestStatus:
    def test_a_healthy_layout_is_readable(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        docker.add(make_inspect(container_id=PROVIDER_ID, name="gluetun", sandbox_key="/netns/x"))
        docker.add(
            make_inspect(
                container_id="b" * 64,
                name="qbittorrent",
                network_mode=f"container:{PROVIDER_ID}",
            )
        )

        lines = render_status(runtime_for(docker, tmp_path))
        joined = "\n".join(lines)

        assert "provider  gluetun" in joined
        assert "managed   qbittorrent  healthy" in joined

    def test_a_missing_provider_is_the_first_line(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        docker.add(
            make_inspect(
                container_id="b" * 64, name="qbittorrent", network_mode=f"container:{PROVIDER_ID}"
            )
        )

        lines = render_status(runtime_for(docker, tmp_path))

        assert lines[0] == "provider  gluetun  MISSING"


class TestRebuild:
    def test_an_unknown_name_is_refused(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        docker.add(make_inspect(container_id=PROVIDER_ID, name="gluetun", sandbox_key="/netns/x"))

        lines, failed = rebuild_named(runtime_for(docker, tmp_path), "sonarr")

        assert failed
        assert "not a dependent" in lines[0]
        assert "substring" in lines[0]

    def test_a_managed_container_is_rebuilt(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        docker.add(make_inspect(container_id=PROVIDER_ID, name="gluetun", sandbox_key="/netns/x"))
        docker.add(
            make_inspect(
                container_id="b" * 64,
                name="qbittorrent",
                network_mode=f"container:{PROVIDER_ID}",
            )
        )

        lines, failed = rebuild_named(runtime_for(docker, tmp_path), "qbittorrent")

        assert not failed
        assert "rebuilt" in lines[0]
        assert docker.by_name("qbittorrent")["Id"] != "b" * 64  # type: ignore[index]


class TestDoctor:
    def test_a_missing_include_is_a_failure(self, tmp_path: Path) -> None:
        docker = FakeDocker()
        docker.add(make_inspect(container_id=PROVIDER_ID, name="gluetun", sandbox_key="/netns/x"))
        docker.add(
            make_inspect(
                container_id="b" * 64,
                name="qbittorrent",
                network_mode=f"container:{PROVIDER_ID}",
            )
        )

        findings = render_doctor(runtime_for(docker, tmp_path, include=["qbittorrent", "sonarr"]))
        joined = "\n".join(findings.lines)

        assert findings.failed
        assert "FAIL  include" in joined
        assert "sonarr" in joined


class TestImportCommand:
    def test_reads_an_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "rdndc.env"
        env_file.write_text("mastercontname=vpn\nmastercontconcheck=yes\n")

        result = runner.invoke(app, ["import-rdndc", "--env-file", str(env_file)])

        assert result.exit_code == 0
        assert "provider: vpn" in result.stdout
        assert "enabled: true" in result.stdout

    def test_env_format_uses_tetherd_prefix(self, tmp_path: Path) -> None:
        env_file = tmp_path / "rdndc.env"
        env_file.write_text("mastercontname=vpn\n")

        result = runner.invoke(
            app, ["import-rdndc", "--env-file", str(env_file), "--format", "env"]
        )

        assert result.exit_code == 0
        assert "TETHERD_PROVIDER=vpn" in result.stdout

    def test_empty_input_explains_what_to_do(self) -> None:
        result = runner.invoke(app, ["import-rdndc"])

        assert result.exit_code == 1
        assert "no Rebuild-DNDC variables found" in result.stderr


class TestHelp:
    def test_root_help_lists_the_commands(self) -> None:
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        for command in ("run", "status", "rebuild", "doctor", "import-rdndc"):
            assert command in result.stdout

    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0, result.output
        assert "0.1.0" in result.stdout
