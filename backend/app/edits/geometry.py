"""图层 → 画布的变换几何，与 edits/render._place 的渲染语义逐像素一致。

变更任一侧时必须同步另一处，tests/test_geometry.py 用真实渲染 footprint 锁定两者不漂移。
"""

import math

from PIL import Image

from app.edits.mask import to_luma
from app.layers import Layer


def forward(layer: Layer) -> tuple[float, float, float, float, float, float]:
    """层本地像素坐标 → 画布坐标的仿射系数 (a, b, c, d, e, f)。

    x = a*u + b*v + c；y = d*u + e*v + f。复刻 _place 的完整序列：
    镜像/翻转 → 绝对值缩放 → 绕中心顺时针旋转（expand 包围盒）→ 中心定位取整。
    """
    w, h = layer.width, layer.height
    t = layer.transform
    sx, sy, theta = t.scale_x, t.scale_y, math.radians(t.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    # 镜像+缩放：U = sx*u + ox（sx<0 时 ox = -sx*w，即 (w-u)*|sx|）
    ox = -sx * w if sx < 0 else 0.0
    oy = -sy * h if sy < 0 else 0.0
    w1, h1 = w * abs(sx), h * abs(sy)

    # 旋转 expand 后的画布尺寸（Pillow ceil-floor 包围盒），_place 以其中心对齐层中心
    w2, h2 = _expanded_size(w1, h1, cos_t, sin_t)

    left = int(round(t.x + w / 2 - w2 / 2))
    top = int(round(t.y + h / 2 - h2 / 2))

    a = sx * cos_t
    b = -sy * sin_t
    c = left + w2 / 2 + cos_t * (ox - w1 / 2) - sin_t * (oy - h1 / 2)
    d = sx * sin_t
    e = sy * cos_t
    f = top + h2 / 2 + sin_t * (ox - w1 / 2) + cos_t * (oy - h1 / 2)
    return (a, b, c, d, e, f)


def footprint_corners(layer: Layer) -> list[tuple[float, float]]:
    """层矩形四角在画布上的位置（凸四边形）。"""
    a, b, c, d, e, f = forward(layer)
    return [
        (a * u + b * v + c, d * u + e * v + f)
        for u, v in ((0, 0), (layer.width, 0), (layer.width, layer.height), (0, layer.height))
    ]


def overlaps_rect(corners: list[tuple[float, float]], rect: tuple[int, int, int, int]) -> bool:
    """凸四边形与轴对齐矩形是否相交（分离轴，覆盖旋转/翻转/非等比缩放）。"""
    x0, y0, x1, y1 = rect
    rect_corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    axes = _rect_axes(corners) + [(1.0, 0.0), (0.0, 1.0)]
    for ax, ay in axes:
        quad = [px * ax + py * ay for px, py in corners]
        box = [px * ax + py * ay for px, py in rect_corners]
        if max(quad) < min(box) or min(quad) > max(box):
            return False
    return True


def to_layer_mask(
    mask: bytes,
    layer: Layer,
    canvas: tuple[int, int],
    *,
    resample: Image.Resampling = Image.Resampling.BILINEAR,
) -> Image.Image:
    """画布坐标选区（PNG 遮罩）逆变换到层本地坐标的 L 模式遮罩。

    PIL transform 的仿射系数是「输出像素坐标 → 输入取样坐标」，
    输出是层本地、输入是画布，恰好就是 forward 系数。
    羽化合成用默认 BILINEAR（软边）；严格像素域校验用 NEAREST
    （最近邻不会把非零值渗入画布遮罩为零的位置）。
    """
    return to_layer_mask_luma(to_luma(mask, canvas), layer, canvas, resample=resample)


def to_layer_mask_luma(
    luma: Image.Image,
    layer: Layer,
    canvas: tuple[int, int],
    *,
    resample: Image.Resampling = Image.Resampling.BILINEAR,
) -> Image.Image:
    """画布坐标 L 遮罩逆变换到层本地（to_layer_mask 的图像入参版本）。"""
    if luma.size != canvas:
        luma = luma.resize(canvas, Image.Resampling.NEAREST)
    fa, fb, fc, fd, fe, ff = forward(layer)
    return luma.transform(
        (layer.width, layer.height),
        Image.Transform.AFFINE,
        (fa, fb, fc, fd, fe, ff),
        resample=resample,
    )


def _expanded_size(w: float, h: float, cos_t: float, sin_t: float) -> tuple[float, float]:
    """旋转 expand 后的画布尺寸，与 Pillow rotate(expand=True) 的 ceil-floor 公式一致。"""
    xs, ys = [], []
    for px, py in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        xs.append(cos_t * px - sin_t * py)
        ys.append(sin_t * px + cos_t * py)
    return (math.ceil(max(xs)) - math.floor(min(xs)), math.ceil(max(ys)) - math.floor(min(ys)))


def _rect_axes(corners: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """四边形两组对边的法线（矩形只需两条轴）。"""
    (x0, y0), (x1, y1), (_x2, _y2), (x3, y3) = corners
    axes = []
    for ex, ey in ((x1 - x0, y1 - y0), (x3 - x0, y3 - y0)):
        length = math.hypot(ex, ey)
        if length > 1e-9:
            axes.append((-ey / length, ex / length))
    return axes
