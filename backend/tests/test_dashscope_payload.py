"""DashScope 请求体与 planner 构造的契约回归测试。

模型名本身是配置替换（无 Red 阶段，见 WI-20261019-01），此处锁定请求体
关键字段与 planner 参数，防止后续模型换代时契约漂移。
"""

import base64

import pytest

from app.agent import llm
from app.config import get_settings
from app.providers.base import EditRequest
from app.providers.dashscope import DashScopeImageProvider


@pytest.fixture
async def provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "dashscope_api_key", "test-key")
    instance = DashScopeImageProvider()
    yield instance
    await instance._client.aclose()


def test_edit_payload_uses_configured_model(provider):
    settings = get_settings()
    payload = provider._edit_payload(EditRequest(prompt="变亮", image=b"png-bytes", count=2))

    assert payload["model"] == settings.image_edit_model
    message = payload["input"]["messages"][0]
    assert message["role"] == "user"
    encoded = base64.b64encode(b"png-bytes").decode()
    assert message["content"][0] == {"image": f"data:image/png;base64,{encoded}"}
    assert message["content"][1] == {"text": "变亮"}
    assert payload["parameters"]["n"] == 2
    assert payload["parameters"]["watermark"] is False
    # 未给宽高或负面提示词时不传对应字段，交给服务端默认值
    assert "size" not in payload["parameters"]
    assert "negative_prompt" not in payload["parameters"]


def test_edit_payload_negative_prompt(provider):
    payload = provider._edit_payload(
        EditRequest(prompt="变亮", image=b"x", negative_prompt="模糊")
    )
    assert payload["parameters"]["negative_prompt"] == "模糊"


@pytest.mark.parametrize(
    "width,height", [(1024, 768), (3000, 100), (100, 3000), (300, 200)]
)
def test_edit_size_clamped_to_model_range(provider, width, height):
    payload = provider._edit_payload(
        EditRequest(prompt="变亮", image=b"x", width=width, height=height)
    )
    w, h = (int(v) for v in payload["parameters"]["size"].split("*"))
    # 编辑接口约束：边长必须在 [512, 2048]
    assert 512 <= w <= 2048
    assert 512 <= h <= 2048


def test_planner_binds_configured_model_without_thinking(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    llm.planner.cache_clear()
    try:
        bound = llm.planner().bound  # bind_tools 返回 RunnableBinding，bound 才是 ChatOpenAI
        assert bound.model_name == settings.planner_model
        assert bound.extra_body == {"enable_thinking": False}
    finally:
        llm.planner.cache_clear()
