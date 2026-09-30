"""Tests for visualization styling."""

from PIL import Image

from deskshot.extraction.visualize import (
    get_element_visual_style,
    render_elements_visualization,
    render_occlusion_visualization,
    render_reading_order_visualization,
)


def test_base_style_stays_source_colored_for_occluded_elements() -> None:
    style = get_element_visual_style(
        {
            "source": "app",
            "_occlusion_partially_covered": True,
            "_occlusion_clipped": True,
        }
    )

    assert style["outline"] == (50, 200, 50, 210)
    assert style["fill"] == (0, 0, 0, 0)


def test_render_elements_visualization_does_not_special_case_occluded_boxes() -> None:
    img = Image.new("RGB", (24, 24), (255, 255, 255))
    base = render_elements_visualization(
        img.copy(),
        [
            {
                "type": "",
                "rect": {"x": 8, "y": 4, "w": 10, "h": 12},
                "source": "app",
            }
        ],
    )
    occluded = render_elements_visualization(
        img.copy(),
        [
            {
                "type": "",
                "rect": {"x": 8, "y": 4, "w": 10, "h": 12},
                "source": "app",
                "_occlusion_partially_covered": True,
                "_occlusion_clipped": True,
                "_occlusion_original_rect": {"x": 2, "y": 4, "w": 16, "h": 12},
            }
        ],
    )

    assert list(base.getdata()) == list(occluded.getdata())


def test_render_reading_order_visualization_draws_index_labels() -> None:
    img = Image.new("RGB", (40, 40), (255, 255, 255))
    out = render_reading_order_visualization(
        img.copy(),
        [
            {
                "type": "Button",
                "rect": {"x": 8, "y": 8, "w": 20, "h": 16},
                "source": "app",
                "reading_order_index": 0,
            }
        ],
    )

    assert list(out.getdata()) != list(img.getdata())


def test_render_reading_order_visualization_draws_arrows_between_elements() -> None:
    img = Image.new("RGB", (80, 40), (255, 255, 255))
    out = render_reading_order_visualization(
        img.copy(),
        [
            {
                "type": "Button",
                "rect": {"x": 6, "y": 8, "w": 16, "h": 16},
                "source": "app",
                "reading_order_index": 0,
            },
            {
                "type": "Button",
                "rect": {"x": 56, "y": 8, "w": 16, "h": 16},
                "source": "app",
                "reading_order_index": 1,
            },
        ],
    )

    assert out.getpixel((40, 16)) != (255, 255, 255)


def test_render_occlusion_visualization_draws_base_and_fragments() -> None:
    img = Image.new("RGB", (80, 60), (255, 255, 255))
    out = render_occlusion_visualization(
        img.copy(),
        [
            {
                "type": "Window",
                "rect": {"x": 10, "y": 10, "w": 50, "h": 30},
                "is_occluded": True,
                "visible_fragments": [
                    {"x": 10, "y": 10, "w": 20, "h": 30},
                    {"x": 40, "y": 10, "w": 20, "h": 30},
                ],
            }
        ],
    )

    assert list(out.getdata()) != list(img.getdata())
