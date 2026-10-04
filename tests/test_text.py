from botco.breakers import Breakers
from botco.config import Breakers as BreakerConfig
from botco.text import check_post, clean_post, mentioned


def test_clean_post_strips_wrapping():
    assert clean_post('"Hello there"') == "Hello there"
    assert clean_post("Post 2: Hello") == "Hello"
    assert clean_post("```\nHello\n```") == "Hello"
    assert clean_post("“Hello”") == "Hello"


def test_check_post():
    assert check_post("A useful tip about KV cache quantization.", 280, []) == []
    assert "too long" in check_post("x" * 281, 280, [])[0]
    assert any("link" in p for p in check_post("see https://example.com", 280, []))
    assert any("link" in p for p in check_post("weights on huggingface.co now", 280, []))
    assert any("mention" in p for p in check_post("thanks @someone", 280, []))
    assert check_post("we run 27B @ 4bpw", 280, []) == []
    assert any("hashtag" in p for p in check_post("#a #b", 280, []))
    assert any("identical" in p for p in check_post("Quantize the KV cache!", 280, ["Quantize the KV cache."]))


def test_mentioned():
    names = {"writer": 10, "editor": 11}
    assert mentioned("hey @**writer** and @**editor|11**", names) == {"writer", "editor"}
    assert mentioned("silent @_**writer**", names) == set()
    assert mentioned("@**someone else**", names) == set()


def test_breakers():
    t = [0.0]
    b = Breakers(BreakerConfig(bot_messages_per_hour=2, bot_streak_per_topic=3), clock=lambda: t[0])
    assert b.allow("writer", "s", "t") is None
    b.record("writer")
    b.record("writer")
    assert "last hour" in b.allow("writer", "s", "t")
    t[0] = 3601
    assert b.allow("writer", "s", "t") is None
    for _ in range(3):
        b.observe("s", "t", from_bot=True)
    assert "in a row" in b.allow("editor", "s", "t")
    b.observe("s", "t", from_bot=False)
    assert b.allow("editor", "s", "t") is None
