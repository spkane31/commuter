from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_temporal_worker_uses_cloudflare_workflow_and_queue_contract():
    source = (ROOT / "src/commuter/temporal_worker.py").read_text()
    workflow = (ROOT / "src/commuter/temporal_workflows.py").read_text()
    assert 'TASK_QUEUE = "commuter-strava"' in source
    assert '@workflow.defn(name="processStravaEvent")' in workflow
    assert "async def process_strava_event" in source


def test_cloudflare_receiver_publishes_events_without_temporal_sdk():
    source = (ROOT / "cloudflare/strava-webhook/src/index.ts").read_text()
    config = (ROOT / "wrangler.jsonc").read_text()
    assert "env.commuter.send" in source
    assert "@temporalio/client" not in source
    assert "Connection.connect" not in source
    assert '"binding": "commuter"' in config
    assert '"queue": "commuter"' in config


def test_make_installs_and_enables_temporal_worker_service():
    makefile = (ROOT / "Makefile").read_text()
    unit = ROOT / "deploy/systemd/commuter-temporal-worker.service"
    assert "install-temporal-worker" in makefile
    assert "enable" in makefile
    assert unit.is_file()
    contents = unit.read_text()
    assert "ExecStart=" in contents and "commuter temporal-worker" in contents
    assert "Restart=on-failure" in contents


def test_env_example_documents_temporal_worker_settings():
    example = (ROOT / "deploy/systemd/commuter.env.example").read_text()
    assert "TEMPORAL_ADDRESS=" in example
    assert "TEMPORAL_NAMESPACE=" in example
    assert "TEMPORAL_API_KEY=" in example
    assert "CLOUDFLARE_ACCOUNT_ID=" in example
    assert "CLOUDFLARE_QUEUE_ID=" in example
    assert "CLOUDFLARE_API_TOKEN=" in example
