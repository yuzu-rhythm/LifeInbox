from life_inbox.normalization import category_for_merchant, normalize_merchant


def test_default_aliases():
    assert normalize_merchant("マック") == "マクドナルド"
    assert normalize_merchant("マクド") == "マクドナルド"
    assert normalize_merchant("セブン") == "セブン-イレブン"
    assert normalize_merchant("コメダ") == "コメダ珈琲店"


def test_category_rules_are_conservative():
    assert category_for_merchant("マクドナルド") == ("食費", "外食")
    assert category_for_merchant("電車") == ("交通", None)
    assert category_for_merchant("知らない店") == ("未分類", None)
