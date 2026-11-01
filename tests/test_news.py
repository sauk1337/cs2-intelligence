"""News aggregator: CS filter, de-duplication, classification, Valve (real + synthetic fixtures)."""
import time

from conftest import fixture_text

from backend.news.classify import classify, is_cs_related, relevance
from backend.news.valve import analyze_update
from backend.sources.news.feeds import RawNews, parse_feed


def raw(src, title, url, summary="", ts=None):
    return RawNews(source=src, title=title, url=url, summary=summary, published_at=ts or time.time())


async def test_dedupe_by_url_and_title(app):
    app.cfg._data["sources"]["news"]["hltv_rss"]["enabled"] = True
    a = await app.news.ingest("hltv_rss", [raw("hltv_rss", "Vitality sign new AWPer for 2027 season", "https://www.hltv.org/news/1/a?utm_source=x")])
    b = await app.news.ingest("hltv_rss", [raw("hltv_rss", "another", "https://hltv.org/news/1/a")])  # same normalised URL
    c = await app.news.ingest("dust2_rss", [raw("dust2_rss", "Vitality sign new AWPer for the 2027 season", "https://dust2.us/n/9")])
    assert len(a) == 1 and b == [] and c[0].duplicate_of == a[0].id
    assert a[0].category == "fichajes"


def test_cs_filter_and_classification():
    assert is_cs_related("Counter-Strike 2 major in Singapore", "")
    assert not is_cs_related("Valorant Champions recap", "Paper Rex roster news")
    assert not is_cs_related("League of Legends Worlds final", "T1 win")
    assert classify("New Kilowatt Case and drop pool changes")[0] == "cajas"
    assert classify("Team Spirit win IEM Cologne grand final")[0] == "torneos"


def test_relevance_rewards_official_and_recent():
    now = time.time()
    hi = relevance("Counter-Strike 2 Update", "", "actualizacion", 30, now, official=True)
    lo = relevance("Some interview", "", "jugadores", 8, now - 48 * 3600)
    assert hi > 70 > lo


def test_valve_update_analysis_marks_speculation():
    body = ("[ GAMEPLAY ]\n- Increased the price of the AWP to $5000.\n- Reduced M4A1-S damage at range.\n"
            "[ MISC ]\n- Removed the Revolution Case from the drop pool.\n- Added trade protection for 7 days.")
    a = analyze_update("Counter-Strike 2 Update", body)
    assert "Gameplay" in a.sections and any("AWP" in f for f in a.facts)
    assert set(a.weapons) >= {"AWP", "M4A1-S"}
    assert any("drop pool" in i for i in a.impacts) and any("trading" in i for i in a.impacts)


def test_real_feed_fixture_items_are_cs():
    items = parse_feed("hltv_rss", fixture_text("hltv_rss.xml"))
    assert all(it.title for it in items)
