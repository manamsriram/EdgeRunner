from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

def test_health_check_get():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_health_check_head():
    response = client.head("/")
    assert response.status_code == 200
    # HEAD requests do not return a body
    assert response.content == b""
