import html
import re


def google_accounts(dumpsys_account: str) -> list[str]:
    """Google account names from `dumpsys account`: the device's own record of sign-in,
    independent of what any screen shows."""
    return list(dict.fromkeys(
        re.findall(r"Account \{name=([^,}]+), type=com\.google\}", dumpsys_account)
    ))


# An IANA time zone as browsers report it ("America/New_York", "Etc/GMT+5", "UTC"): checked before it goes
# into a phone command.
TIME_ZONE = r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$"
# A BCP 47 language tag as browsers report it ("en-US", "zh-Hant-TW").
LANGUAGE = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$"


def validate_package(package):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        raise ValueError("Invalid Android package name")


def ui_nodes(xml: str) -> list[dict]:
    """The nodes of a `uiautomator dump`, each a dict of its attributes (text, content-desc, class, package,
    password, ...) plus `center`, the (x, y) to tap. Nodes without bounds are left out."""
    found = []
    for m in re.finditer(r"<node [^>]*>", xml):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))
        box = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if box:
            x1, y1, x2, y2 = map(int, box.groups())
            attrs["center"] = ((x1 + x2) // 2, (y1 + y2) // 2)
            for key in ("text", "content-desc"):
                if key in attrs:
                    attrs[key] = html.unescape(attrs[key])
            found.append(attrs)
    return found
