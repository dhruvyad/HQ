"""The link an agent writes in its chat when it mentions an item, so one click opens that item.

In a terminal on a Mac with HQ Links (opener.py), it is hq://i/<id>: macOS hands it to HQ Links,
which opens the item in the pill without a browser, and focus stays in the terminal. Anywhere
else it is http to this Mac's own HQ, which every chat makes clickable: a desktop chat app may
refuse other kinds of link. HQ answers that one by opening the item in the pill and closing the
browser tab it came through, or by opening the dashboard on it (see Handler.open_link).
"""
import json
import os
import re
from urllib.parse import unquote, urlsplit

from . import opener
from .store import data_dir

DEFAULT_PORT = 4310
# Eight hex digits tell one item from another among many thousands; the whole id works too.
SHORT = 8
# An id, or the start of one: what a link carries and what someone types.
REFERENCE = re.compile(r"[0-9a-f][0-9a-f-]{5,35}")


def port():
    """The port HQ runs on, or the one it starts on when it isn't running."""
    try:
        value = json.loads((data_dir() / "server.json").read_text()).get("port")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_PORT
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536 else DEFAULT_PORT


def direct():
    """Whether hq:// links work for this chat: HQ Links is on this Mac, and the chat is in a
    terminal, which makes them clickable. Terminal.app may not, so it keeps http."""
    terminal = os.environ.get("TERM_PROGRAM", "")
    return bool(terminal) and terminal != "Apple_Terminal" and opener.ready()


def item_link(item_id, at=None, native=None):
    if native if native is not None else direct():
        return f"hq://i/{item_id[:SHORT]}"
    return f"http://127.0.0.1:{at or port()}/i/{item_id[:SHORT]}"


def link_base(at=None, native=None):
    """A link without its id, to tell an agent how links look here."""
    return item_link("", at, native)


def markdown(title, item_id, at=None, native=None):
    """[title](link), with the brackets a title may hold kept from ending the label early."""
    label = " ".join(str(title).split()).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({item_link(item_id, at, native)})"


def reference(text):
    """The id or start of one in what was typed or pasted: an id, or a link to an item."""
    text = (text or "").strip()
    if "://" in text:
        parts = urlsplit(text)
        # hq://i/<id> puts the i where a host would be.
        text = parts.netloc + parts.path if parts.scheme.lower() == "hq" else parts.path
    path = "/" + unquote(text).strip("/").lower()
    if "/i/" in path:
        path = path.rsplit("/i/", 1)[1]
    ref = path.strip("/")
    if not REFERENCE.fullmatch(ref):
        raise ValueError("Give an item's id, the start of it, or its HQ link")
    return ref


def with_links(value, at=None):
    """The same result, with hq_link on everything in it that is an item or is about one.

    An item carries its id, title and session; a reply, comment or wait carries item_id.
    The outermost of these carries the link, so an item's own comments don't repeat it."""
    at, native = at or port(), direct()

    def walk(node):
        if isinstance(node, list):
            return [walk(entry) for entry in node]
        if not isinstance(node, dict):
            return node
        target = node.get("item_id")
        if not isinstance(target, str) and {"title", "session_id"} <= node.keys():
            target = node.get("id")
        if isinstance(target, str) and target:
            return {**node, "hq_link": item_link(target, at, native)}
        return {key: walk(entry) for key, entry in node.items()}

    return walk(value)
