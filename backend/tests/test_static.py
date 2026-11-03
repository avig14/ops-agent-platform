"""Production serves the built React app from FastAPI. API routes must always win; unknown paths fall back to index.html."""
import httpx
from fastapi import FastAPI

from app import main


def site(tmp_path, with_assets=True):
    (tmp_path / "index.html").write_text("<html>APP</html>", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg>ICON</svg>", encoding="utf-8")
    if with_assets:
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"pong": True}

    assert main.mount_frontend(app, tmp_path) is True
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_spa_fallback_assets_and_api_precedence(tmp_path):
    async with site(tmp_path) as c:
        assert (await c.get("/api/ping")).json() == {"pong": True}                     # real API route wins
        r = await c.get("/")
        assert r.status_code == 200 and "APP" in r.text and r.headers["cache-control"] == "no-cache"
        assert "APP" in (await c.get("/cases/CASE-0001")).text                          # client-side route -> index.html
        assert "console.log" in (await c.get("/assets/app-abc123.js")).text             # hashed asset
        assert "ICON" in (await c.get("/favicon.svg")).text                             # file at the dist root


async def test_unknown_api_path_is_a_json_404_not_the_html_page(tmp_path):
    async with site(tmp_path) as c:
        r = await c.get("/api/nope")
        assert r.status_code == 404 and r.json()["error"] == "not found" and "request_id" in r.json()
        assert (await c.get("/api")).status_code == 404


async def test_path_traversal_cannot_read_files_outside_the_build(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    async with site(dist) as c:
        for p in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
            r = await c.get(p)
            assert "TOP SECRET" not in r.text, p


def test_no_build_means_nothing_is_mounted(tmp_path):
    app = FastAPI()
    assert main.mount_frontend(app, tmp_path / "missing") is False
    assert not [r for r in app.routes if getattr(r, "path", "") == "/{path:path}"]
