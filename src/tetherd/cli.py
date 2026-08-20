"""The operator interface: run, inspect, repair, diagnose, migrate.

Every command that can change a container takes the instance lock first. Status
and doctor are read-only and do not, so they still work while the daemon is
running — which is when you want them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError

from . import __version__
from .assess import assess
from .config import Settings
from .daemon import AlreadyRunningError, Daemon, Runtime, assemble
from .discovery import discover
from .docker_api import DockerUnavailableError
from .log import configure
from .migrate import collect_rdndc, translate_rdndc
from .models import ContainerInfo
from .notify import describe_unavailable
from .provider import ProviderHealth
from .unraid import DEFAULT_TEMPLATE_DIR, audit_templates, describe_integration, is_unraid_host

app = typer.Typer(
    name="tetherd",
    help="Keep containers attached to the network of the container they route through.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        _die(_format_validation(exc))


def _die(message: str, code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    raise typer.Exit(code)


def _runtime(*, dry_run: bool = False) -> Runtime:
    settings = _load_settings()
    if dry_run:
        settings = settings.model_copy(update={"dry_run": True})
    configure(settings.log_level, settings.log_format)
    try:
        return assemble(settings)
    except DockerUnavailableError as exc:
        _die(str(exc))


def _version_callback(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    _version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    return


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="Reconcile once and exit, without watching."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be done, changing nothing."
    ),
) -> None:
    """Watch the provider and repair dependents as they lose its network."""
    runtime = _runtime(dry_run=dry_run)
    try:
        if once:
            with runtime.lock:
                report = runtime.reconciler.run_once()
            for line in render_report(report):
                print(line)
            if report.failures:
                raise typer.Exit(1)
            return
        Daemon(
            runtime.settings,
            runtime.api,
            runtime.reconciler,
            runtime.notifier,
            lock=runtime.lock,
        ).run()
    except AlreadyRunningError as exc:
        _die(str(exc))
    finally:
        runtime.close()


@app.command()
def status() -> None:
    """Show the provider and every dependent, without changing anything."""
    runtime = _runtime()
    try:
        for line in render_status(runtime):
            print(line)
    finally:
        runtime.close()


@app.command()
def rebuild(
    name: str = typer.Argument(help="Exact container name. No substring matching."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the create request without applying it."
    ),
) -> None:
    """Rebuild one dependent from its recorded configuration, even if it looks healthy."""
    runtime = _runtime(dry_run=dry_run)
    try:
        with runtime.lock:
            result_lines, failed = rebuild_named(runtime, name)
        for line in result_lines:
            print(line)
        if failed:
            raise typer.Exit(1)
    except AlreadyRunningError as exc:
        _die(str(exc))
    finally:
        runtime.close()


@app.command()
def doctor() -> None:
    """Check configuration, Docker, templates and notification channels."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(_format_validation(exc))
        raise typer.Exit(1) from exc

    configure(settings.log_level, settings.log_format)
    try:
        runtime = assemble(settings)
    except DockerUnavailableError as exc:
        print(f"FAIL  docker  {exc}")
        raise typer.Exit(1) from exc

    try:
        findings = render_doctor(runtime)
        for line in findings.lines:
            print(line)
        if findings.failed:
            raise typer.Exit(1)
    finally:
        runtime.close()


@app.command("import-rdndc")
def import_rdndc(
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="KEY=value file from a Rebuild-DNDC container. Defaults to this process environment.",
    ),
    fmt: str = typer.Option(
        "yaml",
        "--format",
        help="yaml (for TETHERD_CONFIG_FILE) or env (for Unraid template fields).",
    ),
) -> None:
    """Translate a Rebuild-DNDC configuration into Tetherd's."""
    if fmt not in {"yaml", "env"}:
        _die("--format must be yaml or env")
    environ = _parse_env_file(env_file) if env_file is not None else collect_rdndc(os.environ)
    if not environ:
        _die(
            "no Rebuild-DNDC variables found. Pass --env-file with the old container's "
            "environment, or run this inside a Rebuild-DNDC container."
        )
    translation = translate_rdndc(environ)
    print(translation.as_yaml() if fmt == "yaml" else translation.as_env(), end="")


# -- rendering -------------------------------------------------------------


class DoctorReport:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = False

    def ok(self, topic: str, detail: str) -> None:
        self.lines.append(f"OK    {topic}  {detail}")

    def warn(self, topic: str, detail: str) -> None:
        self.lines.append(f"WARN  {topic}  {detail}")

    def fail(self, topic: str, detail: str) -> None:
        self.failed = True
        self.lines.append(f"FAIL  {topic}  {detail}")


def render_status(runtime: Runtime) -> list[str]:
    known = runtime.state.load().ids
    discovery = discover(runtime.api, runtime.settings, known_provider_ids=known)
    lines: list[str] = []

    if discovery.provider is None:
        lines.append(f"provider  {runtime.settings.provider}  MISSING")
        lines.append("nothing can be repaired until it exists")
        return lines

    provider = discovery.provider
    status = runtime.monitor.check(provider)
    running = "running" if provider.running else "stopped"
    lines.append(f"provider  {provider.name}  {provider.id[:12]}  {running}  {status.health}")
    if status.health is not ProviderHealth.HEALTHY:
        lines.append(f"          {status.detail}")

    adopted = {container.name for container in discovery.adopted}
    if not discovery.managed:
        lines.append("managed   (none)")
    for container in discovery.managed:
        verdict = assess(container, provider)
        snapshot = runtime.snapshots.latest(container.name)
        age = snapshot.age if snapshot else "no snapshot yet"
        tag = "adopted" if container.name in adopted else verdict.verdict
        lines.append(f"managed   {container.name}  {tag}  {age}")
        lines.append(f"          {verdict.reason}")

    for skipped in discovery.skipped:
        lines.append(f"skipped   {skipped.container.name}  {skipped.reason}  {skipped.detail}")

    for name in discovery.unresolved_includes:
        lines.append(f"missing   {name}  named in include but not borrowing the provider's network")

    return lines


def render_report(report: object) -> list[str]:
    from .reconcile import ReconcileReport

    assert isinstance(report, ReconcileReport)
    lines: list[str] = []
    for note in report.notes:
        lines.append(note)
    for result in report.results:
        mark = "ok" if result.succeeded else "FAILED"
        lines.append(f"{mark}  {result.container}  {result.action}  {result.detail}")
    if not report.acted:
        lines.append(f"nothing to do ({len(report.discovery.managed)} managed)")
    return lines


def rebuild_named(runtime: Runtime, name: str) -> tuple[list[str], bool]:
    known = runtime.state.load().ids
    discovery = discover(runtime.api, runtime.settings, known_provider_ids=known)
    if discovery.provider is None:
        return [f"the provider {runtime.settings.provider!r} does not exist"], True

    target = next((c for c in discovery.managed if c.name == name), None)
    if target is None:
        skipped = next((s for s in discovery.skipped if s.container.name == name), None)
        if skipped is not None:
            return [f"{name} is not managed: {skipped.detail}"], True
        return [
            f"{name} is not a dependent of {runtime.settings.provider}. "
            "Names are matched in full; this is not a substring search."
        ], True

    result = runtime.remediator.rebuild(target, discovery.provider)
    return [result.detail], not result.succeeded


def render_doctor(runtime: Runtime) -> DoctorReport:
    report = DoctorReport()
    settings = runtime.settings

    if runtime.api.ping():
        report.ok("docker", f"engine {runtime.api.version()}")
    else:
        report.fail("docker", "the daemon did not respond to ping")

    state_dir = settings.state_dir
    if _writable(state_dir):
        report.ok("state", f"writable at {state_dir}")
    else:
        report.fail("state", f"{state_dir} is not writable; snapshots cannot be kept")

    known = runtime.state.load().ids
    discovery = discover(runtime.api, settings, known_provider_ids=known)

    if discovery.provider is None:
        report.fail("provider", f"{settings.provider} does not exist")
        return report

    provider = discovery.provider
    status = runtime.monitor.check(provider)
    if status.health is ProviderHealth.DOWN:
        report.fail("provider", status.detail)
    elif status.health in (ProviderHealth.UNREACHABLE, ProviderHealth.UNMONITORED):
        report.warn("provider", status.detail)
    else:
        report.ok("provider", f"{provider.name} is {status.health} ({status.source})")

    report.ok("managed", f"{len(discovery.managed)} dependent(s)")
    for skipped in discovery.skipped:
        report.warn("skipped", f"{skipped.container.name}: {skipped.detail}")
    for name in discovery.unresolved_includes:
        report.fail(
            "include",
            f"{name} was named in include but is not borrowing {settings.provider}'s network",
        )

    if is_unraid_host():
        report.ok("unraid", "host marker present")
        _doctor_unraid(report, discovery.managed, settings.provider)
    else:
        report.ok("unraid", "not an Unraid host; template audit skipped")

    if runtime.notifier.configured:
        report.ok("notify", f"channels: {', '.join(runtime.notifier.sink_names)}")
    else:
        report.warn("notify", "no notification channels are available")
    for problem in describe_unavailable(settings.notify):
        report.fail("notify", problem)

    return report


def _doctor_unraid(report: DoctorReport, managed: list[ContainerInfo], provider: str) -> None:
    for container in managed:
        report.ok("labels", f"{container.name}: {describe_integration(container.labels)}")

    if not DEFAULT_TEMPLATE_DIR.is_dir():
        report.warn(
            "templates",
            f"{DEFAULT_TEMPLATE_DIR} is not mounted; Unraid template audit skipped",
        )
        return

    audits = audit_templates((c.name for c in managed), DEFAULT_TEMPLATE_DIR, provider)
    for name, audit in audits.items():
        if audit.is_healthy:
            report.ok("templates", f"{name}: {audit.path.name if audit.path else 'ok'}")
        elif audit.template_missing:
            report.warn(
                "templates", audit.problems[0] if audit.problems else f"{name}: no template"
            )
        else:
            for problem in audit.problems:
                report.fail("templates", problem)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".tetherd-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        return False
    return True


def _format_validation(exc: ValidationError) -> str:
    parts = ["configuration is invalid:"]
    for error in exc.errors():
        location = ".".join(str(bit) for bit in error["loc"]) or "settings"
        parts.append(f"  {location}: {error['msg']}")
    return "\n".join(parts)


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        _die(f"{path} does not exist")
    parsed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip().strip("'\"")] = value.strip().strip("'\"")
    return collect_rdndc(parsed)
