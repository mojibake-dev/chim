"""chim.discord: the support-post renderer + the post-then-edit dispatch (with a fake webhook)."""
import chim.discord as D


def test_render_message_carries_commands_and_sha():
    m = D.render_message("abc123", "vvardenfell.gaussing.tv:25565", "My Canon")
    assert "My Canon" in m
    assert "canon-sync.ps1 -ManifestSha abc123" in m           # windows command, live sha
    assert "MANIFEST_SHA=abc123 sh canon-sync.sh" in m         # posix command, live sha
    assert "vvardenfell.gaussing.tv:25565" in m                # connect target
    assert "abc123" in m


def test_update_posts_first_then_edits_in_place(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": "999"}

    class FakeHook:
        def __init__(self, url, content):
            self.url, self.content, self.id, self.files = url, content, None, []
        def add_file(self, file, filename):
            self.files.append(filename)
        def execute(self):
            calls.append(("execute", self.id, list(self.files))); self.id = "999"; return FakeResp()
        def edit(self):
            calls.append(("edit", self.id, list(self.files))); return FakeResp()

    monkeypatch.setattr(D, "DiscordWebhook", FakeHook)
    files = [("canon-sync.ps1", b"ps"), ("canon-sync.sh", b"sh")]

    # no stored id -> POST, attaching both scripts
    mid = D.update_support_message("http://wh", None, "hi", files)
    assert mid == "999"
    assert calls[0] == ("execute", None, ["canon-sync.ps1", "canon-sync.sh"])

    # stored id -> EDIT the same message (not a repost), re-attaching
    mid2 = D.update_support_message("http://wh", "999", "hi2", files)
    assert mid2 == "999"
    assert calls[1][0] == "edit" and calls[1][1] == "999"


def test_update_reposts_if_message_was_deleted(monkeypatch):
    seq = []

    class Resp404:
        status_code = 404
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": "new"}

    class RespOK:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": "new"}

    class FakeHook:
        def __init__(self, url, content):
            self.id = None
        def add_file(self, file, filename):
            pass
        def execute(self):
            seq.append("execute"); self.id = "new"; return RespOK()
        def edit(self):
            seq.append("edit"); return Resp404()

    monkeypatch.setattr(D, "DiscordWebhook", FakeHook)
    mid = D.update_support_message("http://wh", "gone", "hi", [("a", b"x")])
    assert seq == ["edit", "execute"]   # tried edit, got 404, reposted
    assert mid == "new"
