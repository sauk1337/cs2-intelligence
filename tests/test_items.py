from backend.market.items import parse_market_name, parse_query
from backend.sources.market.base import parse_price


def test_weapon_basic():
    p = parse_market_name("AK-47 | Redline (Field-Tested)")
    assert (p.item_type, p.weapon, p.skin, p.exterior, p.stattrak, p.souvenir) == (
        "weapon", "AK-47", "Redline", "Field-Tested", False, False)
    assert p.short == "AK-47 | Redline (FT)"


def test_stattrak_and_souvenir_are_distinct():
    st = parse_market_name("StatTrak™ AK-47 | Redline (Field-Tested)")
    sv = parse_market_name("Souvenir AWP | Dragon Lore (Factory New)")
    assert st.stattrak and not st.souvenir and st.key != parse_market_name("AK-47 | Redline (Field-Tested)").key
    assert sv.souvenir and sv.weapon == "AWP" and sv.exterior == "Factory New"


def test_knife_gloves_and_phase():
    k = parse_market_name("★ StatTrak™ Karambit | Doppler (Factory New)", "Phase 2")
    assert k.item_type == "knife" and k.stattrak and k.phase == "Phase 2"
    assert k.key.endswith("[Phase 2]")  # a different phase is a different item
    g = parse_market_name("★ Sport Gloves | Vice (Field-Tested)")
    assert g.item_type == "gloves" and g.skin == "Vice"
    assert parse_market_name("★ Karambit").skin == "Vanilla"


def test_other_types():
    assert parse_market_name("Kilowatt Case").item_type == "case"
    s = parse_market_name("Sticker | s1mple (Gold) | Paris 2023")
    assert s.item_type == "sticker" and s.tournament == "Paris 2023"
    assert parse_market_name("Copenhagen 2024 Legends Sticker Capsule").item_type == "capsule"
    assert parse_market_name("Austin 2025 Inferno Souvenir Package").item_type == "souvenir_package"
    assert parse_market_name("Sealed Graffiti | Kisses").item_type == "graffiti"


def test_query_parsing():
    q = parse_query("st ak redline ft")
    assert q.stattrak and q.exterior == "Field-Tested" and q.terms == ["ak", "47", "redline"]
    q = parse_query("AWP | Asiimov (Battle-Scarred)")
    assert q.exterior == "Battle-Scarred" and "asiimov" in q.terms


def test_parse_price_formats():
    assert parse_price("28,37€") == 28.37
    assert parse_price("$1,234.56") == 1234.56
    assert parse_price("1.234,56 zł") == 1234.56
    assert parse_price("28,--€") == 28.0
    assert parse_price("") is None and parse_price(None) is None
    assert parse_price("123.5") == 123.5
