from alerts import DiscordAlertClient


def test_alert_without_webhook_does_not_post(monkeypatch):
    called = False
    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
    monkeypatch.setattr("requests.post", fake_post)
    assert DiscordAlertClient(None).send("hi") is False
    assert called is False


def test_alert_posts_payload(monkeypatch):
    payloads = []
    class Resp:
        def raise_for_status(self): pass
    monkeypatch.setattr("requests.post", lambda url, json, timeout: payloads.append((url, json, timeout)) or Resp())
    assert DiscordAlertClient("https://discord.invalid/webhook").send("hi") is True
    assert payloads[0][1]["content"] == "hi"
