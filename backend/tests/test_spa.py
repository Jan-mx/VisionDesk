import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.fixture(scope="session", autouse=True)
def bucket():
    yield


@pytest.fixture(scope="session", autouse=True)
async def isolated_redis():
    yield


@pytest.fixture(autouse=True)
async def cleanup_users():
    yield


@pytest.mark.parametrize(
    "path",
    ["/auth", "/editor/4ee72360-fe5c-4e7e-811e-693495c0106c", "/batch/run-1", "/candidates/run-1"],
)
async def test_frontend_routes_fall_back_to_index(path: str):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert "<div id=\"root\"></div>" in response.text


async def test_missing_api_and_static_assets_remain_404():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        api = await client.get("/api/does-not-exist")
        asset = await client.get("/assets/does-not-exist.js")

    assert api.status_code == 404
    assert asset.status_code == 404
