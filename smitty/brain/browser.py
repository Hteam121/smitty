"""What a session may do in the user's own browser (Ego Lite, driven by ``ego-browser nodejs`` scripts).

The browser is signed in as the user, so three rules are enforced here in code rather than left to the model:

* **Never** (not even with a spoken yes): payment, checkout, billing and banking pages, placing orders,
  typing card details or passwords, reading cookies or saved sign-in data.
* **Ask every time** ("yes to all" does not cover it): anything that reaches another person or changes
  something: send, post, reply, comment, react, submit, sign up, book, upload, delete.
* **Free**: opening pages, reading, searching, scrolling.

``check_browser_command`` reads the script a session is about to run. Scripts mostly click snapshot refs
("@18"), which say nothing by themselves, so ``BrowserState`` keeps the last snapshot each session printed and
turns a ref back into what it is (``button "Submit order"``). A step that cannot be identified is asked about.

This is a safety net for an honest model making a mistake or being misled by a web page. It reads the scripts
handed to ``ego-browser``; a script hidden inside another program is not seen here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BROWSER_CMD = re.compile(r"\bego-browser\b")
DEFAULT_PROFILE = "Default"          # Chromium's first profile; set SMITTY_BROWSER_PROFILE to use another

# ----------------------------------------------------------------------------- never allowed
ORDER = re.compile(
    r"\b(buy|purchase|check\s?out|pay|pay\s?now|donate|re-?order|order\s+(now|again)|"
    r"(place|submit|confirm|complete|finish|finali[sz]e|send)\s+(your\s+|my\s+|the\s+|this\s+)?(order|purchase|payment)|"
    r"(place|submit)\s+(a\s+|your\s+)?bid|bid\s+now|send\s+money|transfer\s+(money|funds)|withdraw|deposit|"
    r"add\s+funds|top\s+up|1-click|one-click|upgrade\s+(plan|now|to\s+\w+)|subscribe\s+(and|&)\s+pay)\b", re.I)
PAYMENT = re.compile(
    r"(card\s?number|cardnumber|credit\s+card|debit\s+card|\bcc-?(number|num|exp|csc)\b|\bcvv\b|\bcvc\b|security\s+code|"
    r"expir(y|ation)|routing\s+number|account\s+number|bank\s+account|\biban\b|payment\s+methods?|\bpayments?\b|"
    r"\bbilling\b|\bwallet\b|\bpayouts?\b|apple\s+pay|google\s+pay|\bpaypal\b|\bvenmo\b|\bzelle\b|cash\s?app|"
    r"\bssn\b|social\s+security)", re.I)
SECRET = re.compile(r"(pass\s?word|passcode|\bpin\b|\b2fa\b|two-factor|verification\s+code|one-time\s+(code|password)|\botp\b)", re.I)

PAYMENT_HOSTS = (
    "paypal.com", "venmo.com", "cash.app", "wise.com", "zellepay.com", "stripe.com", "squareup.com", "plaid.com",
    "klarna.com", "affirm.com", "afterpay.com", "revolut.com", "monzo.com", "chime.com", "sofi.com", "ally.com",
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "capitalone.com", "americanexpress.com",
    "discover.com", "pnc.com", "td.com", "truist.com", "usaa.com", "navyfederal.org", "synchrony.com",
    "barclays.co.uk", "hsbc.com", "schwab.com", "fidelity.com", "vanguard.com", "robinhood.com", "etrade.com",
    "webull.com", "wealthfront.com", "betterment.com", "coinbase.com", "binance.com", "kraken.com",
    "pay.google.com", "payments.google.com", "passwords.google.com", "wallet.google.com", "wallet.apple.com",
)
PAYMENT_LABEL = re.compile(r"^(bank|banking|pay|payment|payments|wallet|checkout|billing|\w*e?banking|\w+bank)$", re.I)
PAYMENT_PATH = re.compile(r"/(checkout|payments?|billing|wallet|pay|purchase|buy|place-?order|gp/buy|subscriptions?/(new|upgrade))(/|\?|#|$)", re.I)
BROWSER_INTERNAL = re.compile(r"^(chrome|edge|brave|ego|about)://(settings|password|autofill|flags)", re.I)
# Words on a page that mean "this is where you pay": one strong sign plus any second sign makes it a payment
# page. The weak ones alone also appear on order-history pages, which may be read.
PAGE_STRONG = [re.compile(p, re.I) for p in (
    r"card\s?number", r"\bcvv\b|\bcvc\b|security\s+code", r"expir(y|ation)\s+(date|month)|\bMM\s*/\s*YY",
    r"place\s+(your\s+)?order", r"pay\s+now|complete\s+purchase", r"routing\s+number")]
PAGE_WEAK = [re.compile(p, re.I) for p in (r"payment\s+method", r"billing\s+address", r"order\s+(total|summary)")]

# ----------------------------------------------------------------------------- ask every time
COMMUNICATE = re.compile(
    r"\b(send|reply|reply\s+all|forward|post|publish|tweet|retweet|repost|comment|share|invite|message|dm|email|"
    r"respond|rsvp|connect|follow|like|react|upvote|downvote|vote|review|rate|endorse|answer|call|chat|"
    r"accept\s+(the\s+)?(invit\w+|request|offer)|decline\s+(the\s+)?(invit\w+|request|offer))\b", re.I)
SIGN_UP = re.compile(r"\b(subscribe|sign\s?up|register|join|apply|enroll|book|reserve|request\s+(a\s+)?(quote|demo|callback)|start\s+(free\s+)?trial)\b", re.I)
SUBMIT = re.compile(r"\b(submit|confirm|continue\s+to\s+\w+)\b", re.I)
CHANGE = re.compile(
    r"\b(delete|remove|trash|discard|archive|unsubscribe|erase|clear\s+all|empty|block|report|unfriend|unfollow|"
    r"mark\s+as\s+spam|leave\s+(group|channel|server)|revoke|deactivate|close\s+account|sign\s?out|log\s?out|"
    r"save(\s+changes)?|update\s+(profile|settings|account)|change\s+(password|email)|"
    r"cancel\s+(subscription|order|booking|reservation|plan|membership))\b", re.I)
SEARCHY = re.compile(r"\b(search|find|filter|query|look\s?up|address\s+bar|url)\b", re.I)
TEXT_ROLES = ("textbox", "searchbox", "combobox", "textarea", "spinbutton", "input")

# Code that runs inside the page.
PAGE_SECRETS = re.compile(r"document\s*\.\s*cookie|\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|navigator\s*\.\s*credentials|clipboard\s*\.\s*read", re.I)
PAGE_ACTS = re.compile(
    r"\.click\s*\(|\.submit\s*\(|requestSubmit|dispatchEvent|\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|"
    r"\.value\s*=[^=]|location\s*(\.\s*href)?\s*=[^=]|location\s*\.\s*(assign|replace)|window\s*\.\s*open|execCommand|"
    r"\.remove\s*\(|innerHTML\s*=[^=]|postMessage|\.checked\s*=[^=]", re.I)
CDP_SECRETS = re.compile(r"Cookie|Storage\.|Autofill|Credential|WebAuthn", re.I)
WRITE_METHOD = re.compile(r"method\s*:\s*['\"`](POST|PUT|PATCH|DELETE)|['\"]?body['\"]?\s*:", re.I)

CALL = re.compile(
    r"([\w$]+)?\s*\.\s*(click|dblclick|fill|press|focus|selectOption|setInputFiles|setFiles|goto|evaluate|fetch|cdp|"
    r"acceptDialog|saveAs|type|insertText|paste|down|up|snapshot|screenshot)\s*\(")
BARE_CALL = re.compile(r"(?<![\w$.])(fetch|claimTaskSpace|eval)\s*\(|\bnew\s+Function\s*\(")
STRING = re.compile(r"(['\"`])((?:\\.|(?!\1).)*)\1", re.S)
SNAPSHOT_HEAD = re.compile(r'^\[\w+ "([^"]*)" \| space ', re.M)
REF_LINE = re.compile(r"\[ref=(\d+)(?:, loc=([^\]]+))?\]")


@dataclass
class Finding:
    level: str      # "block" | "ask"
    reason: str     # speakable, completes "wants to ..."
    detail: str     # what exactly


@dataclass
class BrowserState:
    """What one session last saw in the browser: enough to tell what a ref points at."""
    refs: dict[str, str] = field(default_factory=dict)    # "18" -> 'button "Submit order"'
    locs: dict[str, str] = field(default_factory=dict)    # 'css:input[name="q"]' -> 'textbox "Search"'
    page: str = ""                                        # host/path of the page in the last snapshot
    payment_page: bool = False
    focus: str = ""                                       # the last thing a script typed into or clicked

    def observe(self, command: str, output: str) -> str | None:
        """Record what a browser script printed. Returns a note for the session when it must leave the page."""
        if not re.search(r"\bego-browser\s+nodejs\b", command):
            return None
        heads = list(SNAPSHOT_HEAD.finditer(output or ""))
        if not heads:
            calls = [c for c in _calls(_script_text(command, None)[0]) if c[1] not in ("snapshot", "screenshot")]
            if calls:
                self.refs, self.locs = {}, {}             # the page may have changed and nothing was printed
            for _, method, args in calls:
                url = _first_string(args) if method == "goto" else None
                if url and not _payment_url(url):         # it navigated away from a payment page
                    self.page, self.payment_page = re.sub(r"^[a-z]+://", "", url), False
            return None
        self.page = heads[-1].group(1)
        body = output[heads[-1].start():]
        self.refs, self.locs = _index_snapshot(body)
        strong = sum(1 for p in PAGE_STRONG if p.search(body))
        weak = sum(1 for p in PAGE_WEAK if p.search(body))
        self.payment_page = bool(_payment_url(self.page)) or (strong >= 1 and strong + weak >= 2)
        if self.payment_page:
            return ("This is a payment, checkout or banking page. Smitty is never allowed to act here. Do not read "
                    "further, fill, click or submit anything on it: send this page to another address with goto() "
                    "now, and tell the user this step is theirs to do.")
        return None


def is_browser_command(command: str) -> bool:
    return bool(BROWSER_CMD.search(command or ""))


def shell_part(command: str) -> str:
    """The command without heredoc bodies, so words inside a browser script are not read as shell commands."""
    return re.sub(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", "", command, flags=re.S | re.M)


# ----------------------------------------------------------------------------- reading the script
def _script_text(command: str, cwd: str | None) -> tuple[str, bool]:
    """The script a command runs, including local files it loads. Second value: part of it could not be read."""
    text, opaque = command, False
    paths = set(re.findall(r"file://(/[^'\"`\s)]+)", command))
    paths |= set(re.findall(r"import\(\s*['\"`](/[^'\"`]+)['\"`]", command))
    paths |= set(re.findall(r"<\s*([^\s<'\"]+\.[cm]?js)\b", shell_part(command)))
    for raw in paths:
        p = Path(raw.replace("%20", " "))
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        try:
            body = p.read_text(errors="replace")[:500_000]
        except OSError:
            opaque = True
            continue
        text += "\n" + body
        if re.search(r"import\s*\(\s*['\"`](?!node:)|^\s*import\s.+from\s+['\"](?!node:)", body, re.M):
            opaque = True
    return text, opaque


def _args(text: str, start: int) -> str:
    """The argument text of a call whose opening parenthesis is at ``start - 1``."""
    depth, i, quote = 1, start, ""
    while i < len(text) and depth:
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def _calls(text: str) -> list[tuple[str, str, str]]:
    """(owner, method, arguments) for each browser call, in order, skipping code that runs inside the page."""
    out, skip = [], 0
    found = [(m.start(), m.group(1) or "", m.group(2), m.end()) for m in CALL.finditer(text)]
    found += [(m.start(), "", m.group(1) or "Function", m.end()) for m in BARE_CALL.finditer(text)]
    for pos, owner, method, end in sorted(found):
        if pos < skip:
            continue
        args = _args(text, end)
        if method in ("down", "up", "type", "insertText", "paste") and owner not in ("keyboard", "mouse"):
            continue
        out.append((owner, method, args))
        if method == "evaluate":
            skip = end + len(args)
    return out


def _first_string(args: str) -> str | None:
    """The first argument when it is a plain string literal."""
    m = STRING.match(args.strip())
    return m.group(2) if m else None


def _label(args: str) -> str:
    m = re.search(r"\blabel\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", args, re.S)
    return m.group(2) if m else ""


def _index_snapshot(body: str) -> tuple[dict[str, str], dict[str, str]]:
    lines = [(len(ln) - len(ln.lstrip(" ")), ln.strip()) for ln in body.splitlines() if ln.strip()]
    refs: dict[str, str] = {}
    locs: dict[str, str] = {}
    for i, (indent, ln) in enumerate(lines):
        m = REF_LINE.search(ln)
        if not m:
            continue
        role = ln.split(" ", 1)[0]
        words = re.findall(r'"([^"]*)"', REF_LINE.sub("", ln))
        j = i + 1
        while j < len(lines) and lines[j][0] > indent:           # what is inside it
            words += re.findall(r'"([^"]*)"', REF_LINE.sub("", lines[j][1]))
            j += 1
        if not any(w.strip("-: ") for w in words):               # unnamed control: use the text next to it
            for span in (range(i - 1, -1, -1), range(j, len(lines))):
                for k in span:
                    if lines[k][0] < indent:
                        break
                    if lines[k][0] == indent and lines[k][1].startswith("text "):
                        words += re.findall(r'"([^"]*)"', lines[k][1])
            words = [w for w in words if w.strip("-: ")]
        seen = list(dict.fromkeys(w.strip() for w in words if w.strip()))[:6]
        desc = (role + " " + " ".join(f'"{w}"' for w in seen)).strip()[:240]
        refs[m.group(1)] = desc
        if m.group(2):
            locs[m.group(2).strip()] = desc
    return refs, locs


def _target(args: str, state: BrowserState | None) -> tuple[str, bool]:
    """What a call acts on, in words. Second value: it could not be identified."""
    sel, label = _first_string(args), _label(args)
    if sel is None:
        return label, not label
    sel = sel.strip()
    ref = re.fullmatch(r"(?:@|ref=)(\d+)", sel)
    if ref:
        known = state.refs.get(ref.group(1)) if state else None
        return " ".join(x for x in (known, label) if x), not (known or label)
    known = state.locs.get(sel.removeprefix("loc=")) if state else None
    return " ".join(x for x in (known, sel, label) if x), False


def _payment_url(url: str) -> str:
    """Why a URL is off limits, or ''."""
    u = url.strip()
    if BROWSER_INTERNAL.search(u):
        return "open the browser's saved passwords or payment settings"
    m = re.match(r"(?:[a-z][\w+.-]*://)?([^/?#\s]+)([^\s]*)", u, re.I)
    if not m:
        return ""
    host, rest = m.group(1).lower().split("@")[-1].split(":")[0], m.group(2)
    if any(host == h or host.endswith("." + h) for h in PAYMENT_HOSTS):
        return "open a payments or banking site"
    if any(PAYMENT_LABEL.match(label) for label in host.split(".")[:-1]):
        return "open a payments or banking site"
    if PAYMENT_PATH.search(rest):
        return "open a checkout or payments page"
    return ""


def _card_number(text: str) -> bool:
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text):
        digits = [int(c) for c in m.group(0) if c.isdigit()]
        total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(reversed(digits)))
        if total % 10 == 0 and len(set(digits)) > 1:
            return True
    return False


def _never(desc: str) -> str:
    """Why acting on something described as ``desc`` is never allowed, or ''."""
    if ORDER.search(desc):
        return "place an order or pay for something"
    if PAYMENT.search(desc):
        return "use payment or billing details"
    return ""


def _commit(desc: str) -> str:
    """Why clicking something needs the user's yes, or ''."""
    if COMMUNICATE.search(desc):
        return "send or post something as you in the browser"
    if SIGN_UP.search(desc):
        return "sign you up or book something in the browser"
    if CHANGE.search(desc):
        return "delete or change something in the browser"
    if SUBMIT.search(desc) and not SEARCHY.search(desc):
        return "submit a form in the browser"
    return ""


def _is_text_field(desc: str) -> bool:
    d = desc.lower()
    return d.startswith(TEXT_ROLES) or bool(re.search(r"\b(input|textarea)\b(?!\[type=.?(submit|button))", d))


# ----------------------------------------------------------------------------- the check
def check_browser_command(command: str, state: BrowserState | None = None, cwd: str | None = None,
                          profile: str = DEFAULT_PROFILE) -> Finding | None:
    """None when the command only looks at pages. Blocks win over questions; the first of each is reported."""
    sub = re.search(r"\bego-browser\s+(\S+)", command)
    verb = sub.group(1) if sub else ""
    if verb in ("skill", "help", "--help", "-h", "--version", "-v", "version"):
        return None
    if verb != "nodejs":
        return Finding("ask", "update or reconfigure the browser" if verb == "upgrade" else "run an unfamiliar browser command",
                       f"ego-browser {verb}")

    text, opaque = _script_text(command, cwd)
    blocks: list[Finding] = []
    asks: list[Finding] = []

    def block(reason: str, detail: str) -> None:
        blocks.append(Finding("block", reason, _clip(detail)))

    def ask(reason: str, detail: str) -> None:
        asks.append(Finding("ask", reason, _clip(detail)))

    if opaque:
        ask("run a browser script that could not be checked", "it loads a file that could not be read")
    for m in re.finditer(r"profileId\s*:\s*(['\"`])(.*?)\1", text):
        if m.group(2) != profile:
            block("use a browser profile other than yours", m.group(2))
    if re.search(r"profileId\s*:\s*(?![\s'\"`])", text):
        ask("pick a browser profile that could not be checked", "profileId is not a plain value")

    calls = _calls(text)
    where = f" on {state.page}" if state and state.page else ""
    focus = state.focus if state else ""
    on_payment_page = bool(state and state.payment_page)

    for n, (owner, method, args) in enumerate(calls):
        first = _first_string(args)
        if on_payment_page:
            if method == "goto" and first and not _payment_url(first):
                on_payment_page = False
            else:
                block("act on a payment or checkout page", f"{method}{where}")
                break

        if method in ("snapshot", "screenshot"):
            continue
        if method == "goto":
            if first is None:
                ask("open an address that could not be checked", args)
            elif (why := _payment_url(first)):
                block(why, first)
            continue

        if method == "evaluate":
            if PAGE_SECRETS.search(args):
                block("read your cookies or saved sign-in data", "page code" + where)
            elif (why := _never(args)):
                block(why, "page code" + where)
            elif PAGE_ACTS.search(args) or not re.match(r"\s*(async\s*)?(\(|function\b|[\w$]+\s*=>|['\"`])", args):
                ask("run code inside the page that clicks, submits or sends", "page code" + where)
            continue

        if method == "fetch":                        # page.fetch runs as the user; a bare fetch runs from this Mac
            if first and (why := _payment_url(first)):
                block(why, first)
            elif WRITE_METHOD.search(args) or first is None:
                ask("send data to a website as you" if owner else "send data to a website", first or "a computed address")
            continue

        if method == "cdp":
            if CDP_SECRETS.search(args):
                block("read or clear your cookies or saved sign-in data", first or "browser protocol")
            else:
                ask("use low-level browser control that could not be checked", first or "browser protocol")
            continue

        if method in ("eval", "Function"):
            ask("run a browser script that could not be checked", "it builds code while running")
            continue
        if method == "claimTaskSpace":
            ask("take over one of your own browser windows", "claimTaskSpace")
            continue
        if method in ("setInputFiles", "setFiles"):
            files = [s.group(2) for s in STRING.finditer(args)]
            ask("upload a file from this Mac", (files[-1] if files else "a file") + where)
            continue
        if method == "saveAs":
            ask("download a file to this Mac", first or "a file")
            continue
        if method == "acceptDialog":
            ask("confirm a pop-up question in the browser", "accept dialog" + where)
            continue

        if owner == "mouse":
            if method != "click" and method != "down":
                continue
            label = _label(args)
            if (why := _never(label)):
                block(why, label + where)
            elif not label:
                ask("click a spot on the page that could not be identified", "a position" + where)
            elif (why := _commit(label)):
                ask(why, label + where)
            focus = label or focus
            continue

        if owner == "keyboard":
            if method in ("type", "insertText", "paste"):
                if _never(focus) or SECRET.search(focus):
                    block("type payment details or a password", focus + where)
                elif _card_number(args):
                    block("type a card number", "a card number" + where)
                elif method == "type" and re.search(r"\\n|\\r", args):
                    _enter(focus, where, block, ask)
            elif method in ("press", "down") and re.search(r"\b(Enter|Return|NumpadEnter)\b", args):
                _enter(focus, where, block, ask)
            continue

        desc, unknown = _target(args, state)
        if method in ("fill", "selectOption"):
            if _never(desc) or SECRET.search(desc):
                block("type payment details or a password", desc + where)
            elif _card_number(args):
                block("type a card number", "a card number" + where)
            elif unknown:
                ask("fill in something on the page that could not be identified", (first or "a field") + where)
            focus = desc or focus
            continue
        if method == "press":
            keys = [s.group(2) for s in STRING.finditer(args)][1:2]
            if keys and re.search(r"\b(Enter|Return|NumpadEnter)\b", keys[0]):
                _enter(desc or focus, where, block, ask)
            focus = desc or focus
            continue
        if method in ("click", "dblclick", "focus"):
            if (why := _never(desc)):
                block(why, desc + where)
            elif unknown:
                ask("click something on the page that could not be identified", (first or "an element") + where)
            elif method != "focus" and not _is_text_field(desc) and (why := _commit(desc)):
                ask(why, desc + where)
            focus = desc or focus

    if state is not None and not blocks:
        state.focus = focus
    return (blocks or asks or [None])[0]


def _enter(focus: str, where: str, block, ask) -> None:
    """Pressing Enter sends whatever was typed. Fine in a search box; anywhere else it needs a yes."""
    if _never(focus) or SECRET.search(focus):
        block("submit payment details or a password", focus + where)
    elif SEARCHY.search(focus) and not COMMUNICATE.search(SEARCHY.sub("", focus)):
        return
    elif COMMUNICATE.search(focus):
        ask("send or post something as you in the browser", (focus or "what it typed") + where)
    else:
        ask("submit what it typed in the browser", (focus or "the current field") + where)


def _clip(s: str, n: int = 110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."
