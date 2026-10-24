"""AER-FR-005：选区命中与遮罩映射使用与画布渲染 _place 一致的变换几何。"""

import io
import uuid

import pytest
from PIL import Image, ImageDraw

from app.edits.geometry import footprint_corners, overlaps_rect, to_layer_mask
from app.edits.render import TRANSPARENT, flatten
from app.layers import Layer, LayerDocument, LayerKind, Transform
from tests.canvas import scene


def solid_png(size: tuple[int, int], color=(200, 30, 30, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_layer(transform: Transform, size: tuple[int, int] = (60, 40)):
    layer = Layer(
        id="subject",
        kind=LayerKind.IMAGE,
        name="主体",
        width=size[0],
        height=size[1],
        asset_id=uuid.uuid4(),
        transform=transform,
    )
    document = LayerDocument(width=200, height=150, layers=[layer])
    return document, layer


TRANSFORM_CASES = [
    pytest.param(Transform(), id="identity"),
    pytest.param(Transform(x=30, y=20), id="move"),
    pytest.param(Transform(x=30, y=20, scale_x=2, scale_y=2), id="scale2"),
    pytest.param(Transform(x=30, y=20, scale_x=0.5, scale_y=1.5), id="nonuniform"),
    pytest.param(Transform(x=30, y=20, scale_x=-1), id="mirror"),
    pytest.param(Transform(x=100, y=75, scale_y=-1.5), id="flip"),
    pytest.param(Transform(x=100, y=75, rotation=45), id="rotate45"),
    pytest.param(Transform(x=100, y=75, rotation=-30, scale_x=1.5), id="rotate-scale"),
    pytest.param(Transform(x=100, y=75, rotation=90, scale_x=-2, scale_y=0.5), id="combo"),
]


@pytest.mark.parametrize("transform", TRANSFORM_CASES)
def test_footprint_corners_match_rendered_alpha(transform: Transform):
    """几何四边形的包围盒必须与真实渲染的 alpha footprint 一致（亚像素容差）。"""
    document, layer = make_layer(transform)
    images = {layer.asset_id: solid_png((layer.width, layer.height))}
    rendered = Image.open(io.BytesIO(flatten(document, images, background=TRANSPARENT)))
    bbox = rendered.getchannel("A").getbbox()
    assert bbox is not None

    corners = footprint_corners(layer)
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    # 渲染会被画布边界裁剪，四边形超出画布的部分对齐到画布界再比较
    quad = (
        max(0.0, min(xs)),
        max(0.0, min(ys)),
        min(float(document.width), max(xs)),
        min(float(document.height), max(ys)),
    )
    for rendered_edge, geometry_edge in zip(bbox, quad):
        assert abs(rendered_edge - geometry_edge) <= 2


def test_footprint_area_survives_rotation():
    """旋转保面积：四边形面积 = 缩放后层面积，不被 expand 包围盒放大。"""
    transform = Transform(x=100, y=75, rotation=45, scale_x=2, scale_y=1)
    _document, layer = make_layer(transform)
    corners = footprint_corners(layer)
    area = abs(
        corners[0][0] * (corners[1][1] - corners[3][1])
        + corners[1][0] * (corners[2][1] - corners[0][1])
        + corners[2][0] * (corners[3][1] - corners[1][1])
        + corners[3][0] * (corners[0][1] - corners[2][1])
    ) / 2
    expected = 60 * abs(transform.scale_x) * 40 * abs(transform.scale_y)
    assert area >= expected * 0.98


def canvas_mask_with_rect(box: tuple[int, int, int, int], canvas=(200, 150)) -> bytes:
    mask = Image.new("L", canvas, 0)
    ImageDraw.Draw(mask).rectangle(box, fill=255)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return buffer.getvalue()


def luma_bbox(data) -> tuple[int, int, int, int] | None:
    if isinstance(data, bytes):
        return to_luma(data).getbbox()
    return data.getbbox()


def test_to_layer_mask_maps_selection_through_scale():
    """层放大 2 倍后，画布选区逆映射到层本地坐标减半（AC-009 核心）。

    _place 语义：x/y 是未缩放框左上角，缩放绕 (x+w/2, y+h/2) 中心。
    层 40×30 scale2 → 有效 80×60，中心 (30,35)，画布 x∈[-10,70]、y∈[5,65]。
    """
    _document, layer = make_layer(Transform(x=10, y=20, scale_x=2, scale_y=2), size=(40, 30))
    mask = canvas_mask_with_rect((20, 30, 50, 60))

    local = to_layer_mask(mask, layer, (200, 150))

    # 画布 (20,30)-(50,60) → 层本地 ((20+10)/2,(30-5)/2)-((50+10)/2,(60-5)/2)
    assert luma_bbox(local) == pytest.approx((15, 12, 30, 27), abs=2)


def test_to_layer_mask_maps_selection_through_mirror():
    """水平翻转层：画布选区 x 越靠右，层本地越靠左（AC-010）。"""
    _document, layer = make_layer(Transform(x=10, y=20, scale_x=-2, scale_y=2), size=(40, 30))
    mask = canvas_mask_with_rect((20, 30, 50, 60))

    local = to_layer_mask(mask, layer, (200, 150))

    # U=(w-u)*|sx|：x=20→u=25，x=50→u=10（左右颠倒）
    assert luma_bbox(local) == pytest.approx((10, 12, 25, 27), abs=2)


def test_to_layer_mask_maps_selection_through_rotation():
    """层顺时针旋转 90°：画布中心上方的选区来自层本地左缘（AC-010）。"""
    layer_transform = Transform(x=80, y=60, scale_x=2, scale_y=2, rotation=90)
    _document, layer = make_layer(layer_transform, size=(40, 30))
    # 层 80×60 顺转 90°，中心 (100,75)，占画布 x∈[70,130]
    mask = canvas_mask_with_rect((90, 40, 100, 50))

    local = to_layer_mask(mask, layer, (200, 150))
    box = luma_bbox(local)
    assert box is not None
    # 选区中心 (95,45) → 层本地 (5,17.5)：u 贴左缘，v 居中
    assert box[0] <= 8 and box[2] <= 11
    assert abs((box[1] + box[3]) / 2 - 17.5) <= 4


def _two_layer_document():
    background = Layer(
        id="background",
        kind=LayerKind.IMAGE,
        name="背景",
        width=200,
        height=150,
        asset_id=uuid.uuid4(),
    )
    subject = Layer(
        id="subject",
        kind=LayerKind.IMAGE,
        name="主体",
        width=40,
        height=30,
        asset_id=uuid.uuid4(),
        transform=Transform(x=80, y=60, scale_x=3, scale_y=3, rotation=45),
    )
    document = LayerDocument(width=200, height=150, layers=[background, subject])
    return document, subject


def test_layer_under_mask_hits_rotated_subject_corner():
    """AC-009：主体旋转后，选区落在旋转伸展出的对角上应命中主体而非背景。

    未变换矩形 (80,60)-(200,150) 不含 (89,1)；旋转 45° 后层左上角伸展到画布 (89,1) 附近。
    """
    from app.tools.target import layer_under_mask

    document, _subject = _two_layer_document()
    mask = canvas_mask_with_rect((84, 0, 96, 12))

    assert layer_under_mask(document, mask).id == "subject"


def test_layer_under_mask_misses_when_outside_diamond_but_inside_expand_box():
    """AC-010：选区在旋转层的 expand 包围盒内、但实际在层四边形外 → 命中下层背景。"""
    from app.tools.target import layer_under_mask

    document, _subject = _two_layer_document()
    # 旋转 45° 后层占 x∈[25,175] 的菱形；左上角 (30,20) 在菱形外（x+y=50 < 90.2 边界）
    mask = canvas_mask_with_rect((25, 15, 35, 25))

    assert layer_under_mask(document, mask).id == "background"


def test_overlaps_rect_handles_flip_and_scale():
    _document, layer = make_layer(Transform(x=50, y=50, scale_x=-2, scale_y=2), size=(40, 30))
    corners = footprint_corners(layer)
    # 有效区域 x∈[30,110]：右端选区相交，左侧远处不相交
    assert overlaps_rect(corners, (100, 60, 112, 72))
    assert not overlaps_rect(corners, (10, 60, 20, 72))
