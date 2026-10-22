"""调用 DashScope qwen-image-3.0 重新生成落地页与海报样例图。

输出暂存至 frontend/public/creative/staging/，供审查确认。
"""

import asyncio
import io
import os
import sys
import time
from pathlib import Path

# 针对 Windows 宿主机环境怪癖做预清理：
# 1. 清理代理环境变量，DashScope 直连避免间歇性宕机
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)

# 2. 清理宿主机残留的失效 DASHSCOPE_API_KEY，确保强制使用项目根目录 .env
os.environ.pop("DASHSCOPE_API_KEY", None)

# 将 backend 根目录加入 sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from PIL import Image

from app.config import get_settings
# 重置并预加载 settings 确保使用 .env 中的配置
get_settings.cache_clear()
settings = get_settings()
print(f"[配置检查] API Key 前缀: {settings.dashscope_api_key[:12]}... 模型: {settings.text_to_image_model}")

from app.providers.base import GenerateRequest
from app.providers.dashscope import DashScopeImageProvider

ASSETS = [
    {
        "id": "serum",
        "title": "植萃精华 · 自然有光",
        "tag": "美妆护肤",
        "prompt": (
            "高端商业静物摄影，一瓶极简磨砂半透明的温润玉白色玻璃滴管精华液，配微磨砂哑光香槟金瓶盖与白色硅胶滴头。"
            "置于温润的象牙白微水泥圆柱形极简展台上。背景为统一协调的暖杏米白与浅奶油色柔和微渐变，"
            "斜射入温暖晨曦自然日光，在台面投下极柔和的半透明阴影与温润光斑，点缀一抹极淡的浅干草与棉麻布纹微景深。"
            "纯净高级，同色系调和，温润透亮，8k超高清商品主图。"
        ),
        "negative_prompt": "浓重冷色，绿色背景，高对比度，刺眼色彩，深色琥珀，塑料感，低分辨率，模糊，杂乱，水印，文字堆砌",
    },
    {
        "id": "sneaker",
        "title": "轻盈每一步",
        "tag": "鞋服配饰",
        "prompt": (
            "极简现代复古运动商业摄影，一只经典复古德训慢跑鞋，配色为温和的燕麦奶白与浅暖沙灰双色翻毛皮拼接，"
            "搭配温润琥珀焦糖色生胶大底（Gum sole）与米白棉质鞋带。平稳置于质感温润的米白色几何展台上，微微悬浮倾斜姿态。"
            "背景为纯净的暖米灰与象牙白影棚柔光无缝背景，侧方暖调柔光箱打出通透立体光影，细腻翻毛皮质感与透气孔微距细节清晰。"
            "温和低饱和暖色系，与复古生活美学融为一体，8k超清画质，干净纯净，无刺眼杂色。"
        ),
        "negative_prompt": "艳丽色彩，紫色，荧光色，冷蓝，杂乱背景，脏乱，低分辨率，模糊，水印，脚，人体，低质粗糙，扭曲",
    },
    {
        "id": "tote",
        "title": "把好心情装进口袋",
        "tag": "生活好物",
        "prompt": (
            "商业生活方式摄影，一只天然米白色重磅棉麻帆布托特包，包身正面带有精致优雅的法式小字体排版印花，"
            "包口自然微敞，露出一束新鲜黄色洋甘菊与法棍面包。放置在温暖奶杏色与蜜桃暖光摄影台面上，"
            "侧面自然窗光洒入，布纹纤维与粗棉绳编织肌理细腻可见。治愈系生活美学，明亮纯净，高级温暖，8k超高清。"
        ),
        "negative_prompt": "褶皱过度，肮脏，破损，低分辨率，模糊，水印，人脸，畸变，低劣质感",
    },
    {
        "id": "coffee",
        "title": "留一点时间，给自己",
        "tag": "家居日用",
        "prompt": (
            "商业静物摄影，一只极具手作质感的雾霾蓝哑光陶瓷马克杯，杯中盛有精美郁金香拉花的热拿铁咖啡。"
            "放置在质感温润的天然浅色白橡木桌面上，百叶窗晨光洒落投射出的柔和线性条纹阴影。"
            "北欧慵懒慢生活氛围，杯身粗陶微颗粒釉面质感极其真实，浅景深虚化自然，温暖治愈，8k超高清。"
        ),
        "negative_prompt": "打翻，溢出，粗糙，低分辨率，模糊，水印，怪异手部，扭曲，暗沉脏乱",
    },
]

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "public" / "creative" / "staging"


async def generate_single(provider: DashScopeImageProvider, item: dict) -> None:
    asset_id = item["id"]
    title = item["title"]
    print(f"\n[🚀 开始生成] {title} ({asset_id})...")
    start_t = time.time()

    req = GenerateRequest(
        prompt=item["prompt"],
        negative_prompt=item["negative_prompt"],
        width=1080,
        height=1350,  # 4:5 纵向商业比例
        count=1,
    )

    async def on_progress(p: int, stage: str):
        print(f"  [{asset_id}] {p}% - {stage}")

    images = await provider.generate(req, on_progress=on_progress)
    if not images:
        raise RuntimeError(f"未能生成图片: {asset_id}")

    raw_bytes = images[0]
    cost_t = time.time() - start_t
    print(f"  [{asset_id}] 生成成功，耗时 {cost_t:.1f}s，开始优化转存...")

    # 保存原始 PNG
    png_path = OUTPUT_DIR / f"{asset_id}.png"
    png_path.write_bytes(raw_bytes)

    # 转换为优质 WebP (高质量且体积适中)
    img = Image.open(io.BytesIO(raw_bytes))
    webp_path = OUTPUT_DIR / f"{asset_id}.webp"
    img.save(webp_path, format="WEBP", quality=92, method=6)

    print(f"  [{asset_id}] 已保存: {webp_path.name} ({webp_path.stat().st_size / 1024:.1f} KB)")


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    provider = DashScopeImageProvider()
    print(f"当前文生图模型: {provider._model}")
    print(f"输出暂存目录: {OUTPUT_DIR}")

    targets = sys.argv[1:]
    items = [a for a in ASSETS if a["id"] in targets] if targets else ASSETS

    print(f"本次生成目标: {[a['id'] for a in items]}")

    for item in items:
        await generate_single(provider, item)

    print("\n[🎉 指定样例图生成完毕！]")
    print(f"请前往 {OUTPUT_DIR} 审查图片。")


if __name__ == "__main__":
    asyncio.run(main())
