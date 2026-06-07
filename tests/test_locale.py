from app.services.locale import parse_locale, pick_list, pick_text


def test_parse_locale_defaults_to_zh() -> None:
    assert parse_locale(None) == "zh"
    assert parse_locale("en") == "en"
    assert parse_locale("fr") == "zh"


def test_pick_text_prefers_locale_specific_value() -> None:
    assert pick_text(zh="中文", en="English", locale="en") == "English"
    assert pick_text(zh="中文", en=None, raw="raw", locale="en") == "raw"
    assert pick_text(zh="中文", en=None, locale="zh") == "中文"


def test_pick_list_locale_order() -> None:
    assert pick_list(zh=["a"], en=["b"], locale="en") == ["b"]
    assert pick_list(zh=["a"], en=[], locale="en") == ["a"]
