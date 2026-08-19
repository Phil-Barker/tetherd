"""Unraid integration: label preservation, and auditing templates without using them."""

from __future__ import annotations

from pathlib import Path

import pytest

from tetherd.unraid import (
    INTEGRATION_LABELS,
    LABEL_ICON,
    LABEL_MANAGED,
    LABEL_SHELL,
    LABEL_WEBUI,
    audit_templates,
    describe_integration,
    is_unraid_host,
    label_drift,
)

FULLY_LABELLED = {
    LABEL_MANAGED: "dockerman",
    LABEL_ICON: "https://example.invalid/icon.png",
    LABEL_WEBUI: "http://[IP]:[PORT:8080]/",
    LABEL_SHELL: "bash",
}


def write_template(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body)
    return path


def container_template(
    name: str, *, network: str = "none", extra_params: str = "", ports: tuple[str, ...] = ()
) -> str:
    port_elements = "".join(
        f'<Config Type="Port" Target="{port}" Mode="tcp">{port}</Config>' for port in ports
    )
    return (
        f"<Container><Name>{name}</Name>"
        f"<Repository>lscr.io/linuxserver/{name}</Repository>"
        f"<Network>{network}</Network>"
        f"<ExtraParams>{extra_params}</ExtraParams>"
        f"{port_elements}</Container>"
    )


class TestLabelPreservation:
    """The guard behind Unraid still recognising a rebuilt container.

    Rebuilding from a template cannot preserve these, because they are not in
    the template: only `managed` was ever re-added by the predecessor, and the
    icon, WebUI link and shell were silently dropped on every rebuild.
    """

    def test_identical_labels_are_no_drift(self) -> None:
        assert label_drift(FULLY_LABELLED, dict(FULLY_LABELLED)) == ()

    @pytest.mark.parametrize("label", INTEGRATION_LABELS)
    def test_a_lost_label_is_reported(self, label: str) -> None:
        after = {k: v for k, v in FULLY_LABELLED.items() if k != label}

        drift = label_drift(FULLY_LABELLED, after)

        assert [change.label for change in drift] == [label]
        assert "was lost" in str(drift[0])

    def test_a_changed_label_is_reported(self) -> None:
        after = {**FULLY_LABELLED, LABEL_MANAGED: "composeman"}

        drift = label_drift(FULLY_LABELLED, after)

        assert [c.label for c in drift] == [LABEL_MANAGED]
        assert "changed from 'dockerman' to 'composeman'" in str(drift[0])

    def test_labels_gained_are_not_drift(self) -> None:
        """A container that had no labels to begin with is not Tetherd's doing."""
        assert label_drift({}, FULLY_LABELLED) == ()

    def test_unrelated_labels_are_ignored(self) -> None:
        before = {**FULLY_LABELLED, "com.example.build": "1"}
        after = {**FULLY_LABELLED, "com.example.build": "2"}

        assert label_drift(before, after) == ()


class TestIntegrationDescription:
    def test_an_unlabelled_container_is_flagged_as_third_party(self) -> None:
        description = describe_integration({})

        assert "3rd Party" in description

    def test_partial_labelling_names_what_is_missing(self) -> None:
        description = describe_integration({LABEL_MANAGED: "dockerman"})

        assert "icon" in description
        assert "WebUI link" in description
        assert "console shell" in description

    def test_a_fully_labelled_container_reports_cleanly(self) -> None:
        assert "icon, WebUI link and console configured" in describe_integration(FULLY_LABELLED)


class TestTemplateAudit:
    def test_templates_are_matched_on_declared_name_not_filename(self, tmp_path: Path) -> None:
        """The fix for upstream issues #77 and #75.

        A filename-based search for 'qbittorrent' also matches
        'my-nordvpn-qbittorrent.xml', which is how the wrong template came to be
        used. Reading the Name element out of the file removes the ambiguity.
        """
        write_template(tmp_path, "my-nordvpn-qbittorrent.xml", container_template("nordvpn"))
        expected = write_template(tmp_path, "my-qbittorrent.xml", container_template("qbittorrent"))

        audits = audit_templates(["qbittorrent"], tmp_path)

        assert audits["qbittorrent"].path == expected

    def test_similarly_named_containers_get_their_own_templates(self, tmp_path: Path) -> None:
        radarr = write_template(tmp_path, "my-radarr.xml", container_template("radarr"))
        radarr_4k = write_template(tmp_path, "my-radarr-4k.xml", container_template("radarr-4k"))

        audits = audit_templates(["radarr", "radarr-4k"], tmp_path)

        assert audits["radarr"].path == radarr
        assert audits["radarr-4k"].path == radarr_4k

    def test_a_healthy_template_reports_no_problems(self, tmp_path: Path) -> None:
        write_template(
            tmp_path,
            "my-app.xml",
            container_template("app", network="none", extra_params="--net=container:gluetun"),
        )

        audit = audit_templates(["app"], tmp_path)["app"]

        assert audit.is_healthy
        assert audit.problems == []

    def test_the_network_element_form_is_recognised(self, tmp_path: Path) -> None:
        """How current Unraid actually writes it, per the 7.3.2 survey.

        Every dependent on the surveyed server declared its provider in the
        Network element, with Extra Parameters carrying only health-check flags.
        """
        write_template(
            tmp_path,
            "my-flaresolverr.xml",
            container_template(
                "flaresolverr",
                network="container:GluetunVPN",
                extra_params=(
                    '--health-cmd="curl -s --max-time 5 --head http://1.1.1.1 || exit 1" '
                    "--health-interval=60s --health-retries=1 --health-start-period=10s"
                ),
            ),
        )

        audit = audit_templates(["flaresolverr"], tmp_path, provider="GluetunVPN")["flaresolverr"]

        assert audit.declared_provider == "GluetunVPN"
        assert audit.is_healthy

    def test_a_template_pointing_at_a_different_provider_is_flagged(self, tmp_path: Path) -> None:
        """An installed container whose template names the wrong provider.

        Pressing Apply in the Docker tab would attach it to a container Tetherd
        is not watching, so it would drop off the network with nothing to fix it.
        """
        write_template(
            tmp_path, "my-app.xml", container_template("app", network="container:old-vpn")
        )

        audit = audit_templates(["app"], tmp_path, provider="GluetunVPN")["app"]

        assert audit.declared_provider == "old-vpn"
        assert any("wrong container" in problem for problem in audit.problems)

    def test_templates_for_uninstalled_apps_are_ignored_entirely(self, tmp_path: Path) -> None:
        """Unraid keeps XML for apps you have removed, so Previous Apps can reinstall them.

        The surveyed server held 82 templates against far fewer containers,
        including one for a long-removed app still wired to a VPN container that
        no longer exists. Those files are history, not configuration: the audit is
        driven by the containers that exist, so it never reads them. Do not
        "improve" this into scanning the directory — every orphan would become a
        warning about something the user deliberately kept.
        """
        write_template(
            tmp_path,
            "my-lazylibrarian.xml",
            container_template("lazylibrarian", network="container:binhex-qbittorrentvpn"),
        )
        write_template(tmp_path, "my-app.xml", container_template("app", network="none"))

        audits = audit_templates(["app"], tmp_path, provider="GluetunVPN")

        assert "lazylibrarian" not in audits
        assert audits["app"].is_healthy

    def test_provider_is_not_checked_when_none_is_configured(self, tmp_path: Path) -> None:
        write_template(tmp_path, "my-app.xml", container_template("app", network="container:other"))

        assert audit_templates(["app"], tmp_path)["app"].is_healthy

    @pytest.mark.parametrize(
        "extra_params",
        [
            "--net=container:gluetun",
            "--network=container:gluetun",
            "--net container:gluetun",
            '--network="container:gluetun" --rm',
            "--cap-add=NET_ADMIN --net='container:gluetun'",
        ],
    )
    def test_every_extra_params_spelling_is_parsed(self, tmp_path: Path, extra_params: str) -> None:
        """Unraid stores Extra Parameters verbatim, so all of Docker's forms appear."""
        write_template(
            tmp_path,
            "my-app.xml",
            container_template("app", network="none", extra_params=extra_params),
        )

        assert audit_templates(["app"], tmp_path)["app"].declared_provider == "gluetun"

    def test_a_template_with_no_borrowed_network_declares_no_provider(self, tmp_path: Path) -> None:
        write_template(tmp_path, "my-app.xml", container_template("app", network="bridge"))

        audit = audit_templates(["app"], tmp_path, provider="GluetunVPN")["app"]

        assert audit.declared_provider is None
        assert audit.is_healthy

    def test_a_missing_template_is_reported(self, tmp_path: Path) -> None:
        audit = audit_templates(["ghost"], tmp_path)["ghost"]

        assert audit.template_missing
        assert not audit.is_healthy
        assert "cannot recreate this container" in audit.problems[0]

    def test_published_ports_alongside_a_borrowed_network_are_flagged(self, tmp_path: Path) -> None:
        """Warns before Unraid destroys the container, which is the whole point.

        Tetherd's own rebuild sanitises this away, but if the user hits Apply in
        the Docker tab, dockerMan uses the template and the daemon rejects it.
        """
        write_template(
            tmp_path,
            "my-app.xml",
            container_template(
                "app", network="none", extra_params="--net=container:gluetun", ports=("8080",)
            ),
        )

        audit = audit_templates(["app"], tmp_path)["app"]

        assert not audit.is_healthy
        assert "conflicting options" in audit.problems[0]
        assert "8080" in audit.problems[0]

    def test_a_network_type_that_contradicts_extra_params_is_flagged(self, tmp_path: Path) -> None:
        """The misconfiguration behind issue #57, open upstream for five years."""
        write_template(
            tmp_path,
            "my-app.xml",
            container_template("app", network="bridge", extra_params="--net=container:gluetun"),
        )

        audit = audit_templates(["app"], tmp_path)["app"]

        assert any("Set Network Type to None" in problem for problem in audit.problems)

    def test_ports_without_a_borrowed_network_are_fine(self, tmp_path: Path) -> None:
        write_template(
            tmp_path, "my-app.xml", container_template("app", network="bridge", ports=("8080",))
        )

        assert audit_templates(["app"], tmp_path)["app"].is_healthy

    def test_an_unparseable_template_does_not_crash_the_audit(self, tmp_path: Path) -> None:
        write_template(tmp_path, "broken.xml", "<Container><Name>app</Name")
        write_template(tmp_path, "my-app.xml", container_template("app"))

        assert audit_templates(["app"], tmp_path)["app"].is_healthy

    def test_a_missing_template_directory_is_survivable(self, tmp_path: Path) -> None:
        """Tetherd must work when the templates volume is not mounted at all."""
        audits = audit_templates(["app"], tmp_path / "nope")

        assert audits["app"].template_missing


class TestHostDetection:
    def test_detects_an_unraid_host_by_its_marker(self, tmp_path: Path) -> None:
        assert is_unraid_host(tmp_path) is True

    def test_absent_marker_means_not_unraid(self, tmp_path: Path) -> None:
        assert is_unraid_host(tmp_path / "nope") is False
