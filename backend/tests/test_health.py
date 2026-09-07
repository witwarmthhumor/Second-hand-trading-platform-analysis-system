"""健康检查接口测试。

阶段 0 验证：FastAPI 应用能启动，/health 返回 200 且结构正确。
数据库连通性依赖容器，这里仅验证接口契约，不依赖真实数据库。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    """正常路径：/health 返回 200，且包含 status 与 database 字段。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "database" in body
