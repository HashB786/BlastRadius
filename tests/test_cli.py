"""CLI smoke tests: exit codes are part of the contract."""

from __future__ import annotations

from blastradius.cli import main


def test_dataset_command_needs_no_database(capsys) -> None:
    assert main(["dataset"]) == 0
    out = capsys.readouterr().out
    assert '"services": 40' in out


def test_health_reports_live_server(capsys) -> None:
    assert main(["health"]) == 0
    out = capsys.readouterr().out
    assert "status      : live" in out


def test_missing_service_exits_with_2(capsys) -> None:
    assert main(["impact", "definitely-not-a-service"]) == 2
    out = capsys.readouterr().out
    assert "not in the graph" in out
    assert "Did you mean" in out


def test_impact_command_prints_a_path(capsys) -> None:
    assert main(["impact", "payment-service", "--depth", "2", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"id": "checkout-service"' in out
