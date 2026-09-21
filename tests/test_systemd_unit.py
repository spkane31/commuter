from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path


def test_local_web_service_template_uses_the_installing_pi_user_and_checkout() -> None:
    unit_path = Path(__file__).parents[1] / "deploy" / "systemd" / "commuter-web.service"
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(unit_path, encoding="utf-8")

    service = parser["Service"]
    assert service["User"] == "__COMMUTER_SERVICE_USER__"
    assert service["Group"] == "__COMMUTER_SERVICE_GROUP__"
    assert service["WorkingDirectory"] == "__COMMUTER_PROJECT_ROOT__"
    assert service["EnvironmentFile"] == "/etc/commuter/commuter.env"
    assert service["ExecStart"] == "__COMMUTER_PROJECT_ROOT__/.venv/bin/commuter"
    assert service["UMask"] == "0077"
    assert service["Restart"] == "on-failure"
    assert service["ProtectSystem"] == "true"
    assert service["ProtectHome"] == "read-only"

    assert parser["Install"]["WantedBy"] == "multi-user.target"


def test_sync_timer_runs_the_local_poller_every_fifteen_minutes() -> None:
    project_root = Path(__file__).parents[1]
    service_parser = ConfigParser(interpolation=None)
    service_parser.optionxform = str
    service_parser.read(project_root / "deploy" / "systemd" / "commuter-sync.service", encoding="utf-8")
    timer_parser = ConfigParser(interpolation=None)
    timer_parser.optionxform = str
    timer_parser.read(project_root / "deploy" / "systemd" / "commuter-sync.timer", encoding="utf-8")

    service = service_parser["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "__COMMUTER_SERVICE_USER__"
    assert service["Group"] == "__COMMUTER_SERVICE_GROUP__"
    assert service["WorkingDirectory"] == "__COMMUTER_PROJECT_ROOT__"
    assert service["EnvironmentFile"] == "/etc/commuter/commuter.env"
    assert service["ExecStart"] == "__COMMUTER_PROJECT_ROOT__/.venv/bin/commuter sync"
    assert service["UMask"] == "0077"
    assert service["ProtectSystem"] == "true"
    assert service["ProtectHome"] == "read-only"

    timer = timer_parser["Timer"]
    assert timer["Unit"] == "commuter-sync.service"
    assert timer["OnBootSec"] == "2min"
    assert timer["OnUnitActiveSec"] == "15min"
    assert timer["Persistent"] == "true"
    assert timer_parser["Install"]["WantedBy"] == "timers.target"
