from fastapi.testclient import TestClient

from mongoose.app import app


def test_system_status_endpoint_shape():
    client = TestClient(app)
    r = client.get("/system/status")
    assert r.status_code == 200
    data = r.json()
    assert "mongoose" in data
    assert "models" in data
    assert "cloud" in data


def test_tools_catalogue_endpoint_lists_allowlist():
    client = TestClient(app)
    r = client.get("/tools/catalogue")
    assert r.status_code == 200
    tools = r.json()["tools"]
    names = {item["name"] for item in tools}
    assert "get_weather" in names
    assert "control_home" in names
    assert "send_email" not in names
    assert "purchase" not in names
