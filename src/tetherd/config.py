"""Configuration, from environment variables and/or a YAML file.

Environment variables are the primary interface because that is all the Unraid
template UI can offer. Every list-valued setting therefore accepts a plain
comma- or whitespace-separated string as well as a JSON array, so users are not
asked to type JSON into a web form.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CONFIG_FILE_ENV_VAR = "TETHERD_CONFIG_FILE"
DEFAULT_CONFIG_FILE = Path("/config/tetherd.yaml")


def _split_loosely(value: Any) -> Any:
    """Accept "a,b", "a b", "a, b" or a JSON array wherever a list is expected."""
    if not isinstance(value, str):
        return value

    candidate = value.strip()
    if candidate.startswith("["):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Fall through and treat it as a delimited string; a malformed JSON
            # array is more likely a typo than a container name.
            pass
    return [item for item in candidate.replace(",", " ").split() if item]


# NoDecode stops pydantic-settings JSON-decoding the raw environment value, so
# _split_loosely sees the string the user actually typed.
LooseList = Annotated[list[str], NoDecode, BeforeValidator(_split_loosely)]


class ProbeSettings(BaseModel):
    """Active connectivity checking of the provider container.

    This is a genuine capability gap in Unraid's native handling: nothing else
    notices that a VPN container is up but its tunnel is dead.
    """

    enabled: bool = False
    targets: LooseList = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    timeout_seconds: float = Field(default=5.0, gt=0)
    failures_before_restart: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive failed probe rounds before the provider is restarted. A "
            "round tries every target, so this counts rounds, not packets - "
            "unlike the predecessor's ping_count, which was documented as a "
            "retry count but was only ever a single ping's packet count."
        ),
    )
    restart_provider_on_failure: bool = True
    settle_seconds: float = Field(
        default=10.0,
        ge=0,
        description="Grace period after restarting the provider before repairing dependents.",
    )
    min_restart_interval_seconds: float = Field(
        default=300.0,
        ge=0,
        description=(
            "Floor on how often the provider may be restarted. Nothing inside a "
            "tunnel can tell a dead tunnel from an ISP outage, and without this "
            "an outage would restart the VPN container in a loop and take every "
            "dependent down with it on each pass."
        ),
    )


class NotifySettings(BaseModel):
    urls: LooseList = Field(
        default_factory=list,
        description="Apprise URLs, e.g. tgram://token/chatid or discord://id/token.",
    )
    unraid: bool = Field(
        default=True,
        description=(
            "Write Unraid .notify files when /tmp/notifications is mounted. "
            "The host PHP notify script cannot run inside this image."
        ),
    )
    unraid_path: Path = Field(
        default=Path("/tmp/notifications"),
        description="Unraid's notification directory on the host, bind-mounted into the container.",
    )
    hook: Path | None = Field(
        default=None,
        description=(
            "Executable run after each remediation, with details passed via TETHERD_* env vars."
        ),
    )
    notify_on_healthy_runs: bool = False


class Settings(BaseSettings):
    """Top-level configuration."""

    model_config = SettingsConfigDict(
        env_prefix="TETHERD_",
        env_nested_delimiter="__",
        extra="ignore",
        yaml_file=None,
    )

    provider: str = Field(
        description="Name or ID of the container whose network the others borrow, e.g. gluetun.",
    )

    include: LooseList = Field(
        default_factory=list,
        description=(
            "If set, only these dependents are managed. Exact names; no substring matching."
        ),
    )
    exclude: LooseList = Field(
        default_factory=list,
        description="Dependents to leave alone entirely.",
    )
    require_label: bool = Field(
        default=False,
        description="Only manage containers labelled tetherd.enable=true.",
    )
    adopt_orphans: bool = Field(
        default=True,
        description=(
            "Manage containers pointing at a container ID that no longer exists. "
            "Such a container cannot start and nothing can claim it, and this is "
            "the state a host is in when someone installs Tetherd to fix it. Turn "
            "off if you run more than one provider, so orphans of the other one "
            "are left alone."
        ),
    )

    reconcile_interval_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Full reconcile cadence, as a safety net behind the event stream.",
    )
    event_debounce_seconds: float = Field(
        default=5.0,
        ge=0,
        description="Quiet period after a provider event before acting, to coalesce bursts.",
    )

    state_dir: Path = Field(
        default=Path("/config"),
        description="Where config snapshots and last-seen provider state are kept.",
    )
    snapshot_retention: int = Field(
        default=5, ge=1, description="Config snapshots retained per container."
    )

    dry_run: bool = Field(
        default=False,
        description="Report what would be done without changing anything.",
    )
    restart_grace_seconds: float = Field(
        default=15.0,
        gt=0,
        description="How long to wait for a repaired container to come up before escalating.",
    )

    docker_host: str | None = Field(
        default=None,
        description="Override the Docker endpoint; defaults to the standard socket.",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    probe: ProbeSettings = Field(default_factory=ProbeSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def provider_state_file(self) -> Path:
        return self.state_dir / "provider-state.json"

    @model_validator(mode="after")
    def _reject_contradictory_scoping(self) -> Settings:
        overlap = sorted(set(self.include) & set(self.exclude))
        if overlap:
            raise ValueError(
                f"these containers are in both include and exclude: {', '.join(overlap)}"
            )
        if self.provider in self.include:
            raise ValueError(f"the provider ({self.provider}) cannot also be a managed dependent")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Environment variables win over the YAML file, which wins over defaults."""
        configured = os.environ.get(CONFIG_FILE_ENV_VAR)
        yaml_path = Path(configured) if configured else DEFAULT_CONFIG_FILE

        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if yaml_path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        sources.append(file_secret_settings)
        return tuple(sources)
