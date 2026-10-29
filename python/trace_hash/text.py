import json
from html.parser import HTMLParser

from . import _native
from .model import feature


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li", "h1", "h2", "tr"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_feature(data: bytes, config, *, code=False, html=False):
    value = data.decode("utf-8-sig", errors="strict")
    if html:
        parser = VisibleText()
        parser.feed(value)
        value = "".join(parser.parts)
    payload = json.loads(_native.fingerprint_text(value, code, config.limits.max_features))
    profile = payload.pop("profile")
    return feature("text", profile, payload)
