"""Tests for merging desktop chrome trees from multiple AT-SPI apps."""

from deskshot.extraction.atspi_walker import _offset_element_indices, _resolve_hierarchy


def test_offset_element_indices_rebases_parent_and_children() -> None:
    prior = [
        {
            "_dom_index": 0,
            "_parent_dom_index": None,
            "_children_dom_indices": [],
            "parent_index": None,
            "children_indices": [],
        },
        {
            "_dom_index": 1,
            "_parent_dom_index": None,
            "_children_dom_indices": [],
            "parent_index": None,
            "children_indices": [],
        },
    ]
    elements = [
        {
            "_dom_index": 0,
            "_parent_dom_index": None,
            "_children_dom_indices": [1],
            "parent_index": None,
            "children_indices": [1],
        },
        {
            "_dom_index": 1,
            "_parent_dom_index": 0,
            "_children_dom_indices": [],
            "parent_index": 0,
            "children_indices": [],
        },
    ]

    _offset_element_indices(elements, len(prior))
    assert elements[0]["_dom_index"] == 2
    assert elements[0]["_children_dom_indices"] == [3]
    assert elements[1]["_dom_index"] == 3
    assert elements[1]["_parent_dom_index"] == 2

    combined = prior + elements
    _resolve_hierarchy(combined)
    assert combined[2]["children_indices"] == [3]
    assert combined[3]["parent_index"] == 2


def test_desktop_chrome_elements_carry_the_name_that_resolves_them() -> None:
    """`_atspi_app_name` is the handle a saved element is resolved back through.

    `populate_visible_text` fetches character geometry by re-resolving the live
    accessible from `(_atspi_app_name, _atspi_path)`. `walk_application` sets
    both; `walk_desktop_chrome` set only the path, so every desktop-chrome
    element was unresolvable and was dropped as `unsupported_partial` the moment
    anything overlapped it. Measured on `incremental_checks/v224_verify/batch`:
    166 of 412 withheld elements were desktop file icons whose labels are
    plainly drawn on the wallpaper.
    """
    import deskshot.extraction.atspi_walker as walker

    class _FakeDesktop:
        def get_child_count(self):
            return 1

        def get_child_at_index(self, _index):
            return _FakeApp()

    class _FakeApp:
        def get_name(self):
            return "caja"

    def _fake_walk(node, elements, *_args, **_kwargs):
        elements.append({
            "role": "icon", "inner_text": "12_Notes",
            "rect": {"x": 0, "y": 0, "w": 49, "h": 69},
            "_parent_dom_index": None, "_children_dom_indices": [],
            "_atspi_path": [0, 3],
            "attrs": {"text_source": "text_iface"},
        })
        return 0

    original_desktop = walker.Atspi.get_desktop
    original_walk = walker._walk_recursive
    original_augment = walker._augment_plank_elements
    original_chrome = walker.is_desktop_chrome
    try:
        walker.Atspi.get_desktop = lambda _i: _FakeDesktop()
        walker._walk_recursive = _fake_walk
        walker._augment_plank_elements = lambda _name, elements, **_kw: elements
        walker.is_desktop_chrome = lambda _name: True
        elements = walker.walk_desktop_chrome(1920, 1080, launched_apps=[])
    finally:
        walker.Atspi.get_desktop = original_desktop
        walker._walk_recursive = original_walk
        walker._augment_plank_elements = original_augment
        walker.is_desktop_chrome = original_chrome

    assert elements, "the fake desktop should have produced one element"
    for elem in elements:
        assert elem["_atspi_app_name"] == "caja"
        assert elem["app_name"] == "caja"
