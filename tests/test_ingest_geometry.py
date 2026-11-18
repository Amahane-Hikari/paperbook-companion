"""Coordinate contracts for vision entities and fallback OCR."""

import json

import pytest

import book_ingest


def semantic_response(entities, **metadata):
    return json.dumps({
        "description": "小鸭在池塘里。",
        "characters": ["小鸭"],
        "scene": "池塘",
        "plot_summary": "小鸭在池塘游泳。",
        "style_anchor": "水彩",
        "entities": entities,
        **metadata,
    })


def test_semantic_geometry_keeps_named_nested_objects_and_filters_duplicates(monkeypatch):
    pond = {"name_cn": "水塘", "name_en": "Pond", "bbox": [0.50, 0.44, 0.93, 0.67]}
    duck = {"name_cn": "小鸭", "name_en": "Duckling", "bbox": [0.60, 0.44, 0.82, 0.585]}
    alias = {"name_cn": "幼鸭", "name_en": "duckling", "bbox": [0.601, 0.44, 0.821, 0.585]}
    another_duck = {"name_cn": "小鸭", "name_en": "Duckling", "bbox": [0.10, 0.44, 0.30, 0.58]}
    badly_scaled = {"name_cn": "苹果", "bbox": [305, 435, 465, 525]}
    response = semantic_response(
        [pond, duck, alias, another_duck, badly_scaled],
        coord_space=book_ingest.COORD_SPACE, bbox_format=book_ingest.BBOX_FORMAT,
    )

    def mock_vision(image, prompt, *, parser, **kwargs):
        assert '"coord_space": "normalized_0_1"' in prompt
        assert '"bbox_format": "xyxy"' in prompt
        return parser(response)

    monkeypatch.setattr(book_ingest, "vision_request", mock_vision)
    entities = book_ingest.analyze_page_semantics("page.jpg")["entities"]
    assert [(item["name_cn"], item["bbox"]) for item in entities] == [
        (item["name_cn"], item["bbox"]) for item in (pond, duck, another_duck)
    ]


@pytest.mark.parametrize("bbox", [
    [False, 0.0, 0.2, 0.4],
    ["0.1", 0.0, 0.2, 0.4],
    [0.1, 0.0, float("inf"), 0.4],
    [0.1, 0.0, 1.01, 0.4],
    [-0.1, 0.0, 0.2, 0.4],
    [0.5, 0.0, 0.2, 0.4],
    [0.1, 0.4, 0.2, 0.4],
])
def test_declared_normalized_bbox_is_never_coerced_or_reordered(bbox):
    with pytest.raises(ValueError):
        book_ingest.normalize_bbox(bbox, strict=True)


@pytest.mark.parametrize("metadata", [
    {"coord_space": "pixel"},
    {"bbox_format": "xywh"},
])
def test_unknown_coordinate_metadata_is_rejected(metadata):
    with pytest.raises(ValueError, match="normalized_0_1 xyxy"):
        book_ingest._parse_semantic_json(semantic_response([], **metadata))


def test_only_invalid_entities_trigger_a_retry():
    response = semantic_response(
        [{"name_cn": "苹果", "bbox": [305, 435, 465, 525]}],
        coord_space=book_ingest.COORD_SPACE, bbox_format=book_ingest.BBOX_FORMAT,
    )
    with pytest.raises(ValueError, match="没有可用"):
        book_ingest._parse_semantic_json(response)


def test_legacy_vision_box_scales_and_ocr_are_still_supported(monkeypatch):
    response = semantic_response([
        {"name_cn": "篮子", "bbox": [290, 430, 490, 620]},
        {"name_cn": "苹果", "bbox": [0.305, 0.435, 0.465, 0.525]},
    ])
    entities = book_ingest._parse_semantic_json(response)["entities"]
    assert [item["bbox"] for item in entities] == [[0.29, 0.43, 0.49, 0.62], [0.305, 0.435, 0.465, 0.525]]

    def mock_vision(image, prompt, *, parser, **kwargs):
        assert "0-1000" in prompt
        return parser('{"lines": [{"text": "小鸭", "bbox": [800, 90, 100, 50]}]}')

    monkeypatch.setattr(book_ingest, "vision_request", mock_vision)
    assert book_ingest._ocr_via_vision("page.jpg") == [
        {"text": "小鸭", "bbox": [0.1, 0.05, 0.8, 0.09], "confidence": None}
    ]
