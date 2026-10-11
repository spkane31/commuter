from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_temporal_worker_uses_cloudflare_workflow_and_queue_contract():
    source = (ROOT / "src/commuter/temporal_worker.py").read_text()
    workflow = (ROOT / "src/commuter/temporal_workflows.py").read_text()
    assert 'TASK_QUEUE = "commuter-strava"' in source
    assert '@workflow.defn(name="processStravaEvent")' in workflow
    assert "async def process_strava_event" in source


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
