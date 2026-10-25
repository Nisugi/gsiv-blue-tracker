"""Runs the viewer's content formatter under Node. Skipped when Node isn't installed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")
COMMON_JS = Path(__file__).resolve().parent.parent / "archiver" / "web" / "static" / "common.js"

MENTIONS = {"users": {"55": "Naijin"}, "channels": {"77": "patch-notes"}, "roles": {"9": "GameMaster"}}


def fmt(text, mentions=MENTIONS):
    script = (
        "globalThis.SITE = {guildId: '1000', label: 'GM'};\n"
        + COMMON_JS.read_text(encoding="utf-8")
        + f"\nprocess.stdout.write(Archive.formatContent({json.dumps(text)}, {json.dumps(mentions)}));"
    )
    return subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout


def test_html_is_escaped():
    out = fmt('<script>alert(1)</script> <img src=x onerror="y">')
    assert "<script>" not in out and "<img" not in out and "&lt;script&gt;" in out


def test_mentions_use_names():
    out = fmt("hey <@55> and <@!56>, see <#77>, ping <@&9>")
    assert "@Naijin" in out and "@unknown-user" in out and "@GameMaster" in out
    assert '#patch-notes</a>' in out and 'href="discord://-/channels/1000/77"' in out


def test_app_url():
    script = ("globalThis.SITE = {guildId: '1000'};\n" + COMMON_JS.read_text(encoding="utf-8")
              + "\nprocess.stdout.write(Archive.appUrl('https://discord.com/channels/1/2/3'));")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout
    assert out == "discord://-/channels/1/2/3"


def test_emoji_and_timestamps():
    out = fmt("nice <:gs4:123> <a:spin:456> at <t:1700000000:D>")
    assert 'src="https://cdn.discordapp.com/emojis/123.webp?size=48"' in out and 'alt=":gs4:"' in out
    assert "emojis/456.gif" in out and "<time" in out and "&lt;t:" not in out


def test_links_stop_at_brackets_and_never_allow_javascript():
    out = fmt("<https://play.net/gs4> and javascript:alert(1)")
    assert 'href="https://play.net/gs4"' in out and "&gt;</a>" not in out
    assert 'href="javascript' not in out


def test_markdown():
    out = fmt("**b** *i* __u__ ~~s~~ ||spoiler||\n> quoted")
    for tag in ("<strong>b</strong>", "<em>i</em>", "<u>u</u>", "<s>s</s>", 'class="spoiler"', 'class="quote"'):
        assert tag in out
