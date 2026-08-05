"""Keep the canon's install/update post current in the Discord support channel.

One message in the support channel (the webhook's own), **edited in place** on every roll via the
``discord-webhook`` library, so friends always see the current manifest sha + both client scripts and
the channel never fills with duplicates. The webhook URL is read from the macOS keychain, never the
repo. Run it as a post-roll step (or ``chim-discord`` standalone); the message id is persisted next to
the roll output so subsequent runs edit rather than repost.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import List, Optional, Tuple

from discord_webhook import DiscordWebhook


def read_keychain(account: str, service: str) -> str:
    """Fetch a secret from the macOS keychain (``security find-generic-password``). Never logged."""
    out = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"no keychain secret for -a {account} -s {service}")
    return out.stdout.strip()


def render_message(sha: str, server: str, title: str) -> str:
    """The support post body: install + update in two commands, one per OS, with the live sha."""
    return (
        f"**{title}** (install + update)\n\n"
        "**First run:** unzip the canon somewhere permanent, then from inside that folder run your "
        "OS's script (attached below). After that just re-run it any time: it pulls only what "
        "changed, fixes your load order and gameplay settings, and updates itself.\n\n"
        "**Windows** (PowerShell):\n"
        f"```\npowershell -ExecutionPolicy Bypass -File canon-sync.ps1 -ManifestSha {sha}\n```\n"
        "**macOS / Linux** (needs `curl` and `jq`):\n"
        f"```\nMANIFEST_SHA={sha} sh canon-sync.sh\n```\n"
        f"Then launch and connect to `{server}` with your invite password.\n\n"
        f"_Manifest SHA-256:_ `{sha}`\n"
        "_This message is rewritten automatically on every canon roll._"
    )


def update_support_message(webhook_url: str, message_id: Optional[str], content: str,
                           files: List[Tuple[str, bytes]]) -> str:
    """Edit the existing support message in place, or post a fresh one. Returns the message id.

    Re-attaches the given files each time (so the pinned copies track the current scripts). If the
    stored message was deleted (404 on edit), posts a new one.
    """
    def _hook() -> DiscordWebhook:
        h = DiscordWebhook(url=webhook_url, content=content)
        for fn, data in files:
            h.add_file(file=data, filename=fn)
        return h

    hook = _hook()
    if message_id:
        hook.id = message_id
        resp = hook.edit()
        if resp.status_code == 404:      # someone deleted it -> repost
            hook = _hook()
            resp = hook.execute()
    else:
        resp = hook.execute()
    resp.raise_for_status()
    return str(hook.id or resp.json().get("id"))


def main(argv=None) -> int:
    """``chim-discord``: refresh the support post from a rolled canon + ``canon.toml``.

    Reads the webhook from the keychain (``[discord] keychain_service`` / ``keychain_account``),
    the manifest sha from the rolled ``modlock.json``, and the two scripts from ``clients/``.
    Persists the message id in ``<out_dir>/.discord_msg_id`` so reruns edit in place.
    """
    import argparse
    import tomllib

    ap = argparse.ArgumentParser("chim-discord", description="refresh the canon support post in Discord")
    ap.add_argument("-c", "--config", default="canon.toml", help="path to canon.toml (default ./canon.toml)")
    args = ap.parse_args(argv)

    with open(args.config, "rb") as fh:
        conf = tomllib.load(fh)
    exp = os.path.expanduser
    out_dir = exp(conf["out_dir"])
    dconf = conf.get("discord", {})
    webhook = read_keychain(dconf.get("keychain_account", "discord"),
                            dconf.get("keychain_service", "canon-discord-webhook"))

    with open(os.path.join(out_dir, "modlock.json"), "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()   # the value a friend pins as MANIFEST_SHA

    clients = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "clients")
    files: List[Tuple[str, bytes]] = []
    for fn in ("canon-sync.ps1", "canon-sync.sh"):
        with open(os.path.join(clients, fn), "rb") as fh:
            files.append((fn, fh.read()))

    content = render_message(sha, conf.get("server_address", "the server"),
                             conf.get("title", "Morrowind Canon"))
    idfile = os.path.join(out_dir, ".discord_msg_id")
    msg_id = open(idfile).read().strip() if os.path.exists(idfile) else None
    was_edit = msg_id is not None

    msg_id = update_support_message(webhook, msg_id, content, files)
    with open(idfile, "w") as fh:
        fh.write(msg_id)
    print(f"discord support message {'edited' if was_edit else 'posted'}: id={msg_id}, sha={sha[:12]}...")
    return 0


__all__ = ["update_support_message", "render_message", "read_keychain", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
