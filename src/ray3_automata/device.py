'''RACOM RAy (RAy2/RAy3) microwave backhaul radio implementation.

Unlike every other device family this author has automated so far
(Ubiquiti, Tachyon), a RAy has no REST/JSON API at all - its entire
management interface, including the CLI tool this module is built
around, is the live web GUI itself: a server-rendered, stateful
single-page app built on the C++ "Wt" toolkit (https://www.webtoolkit.eu/).
Every click is a simulated DOM event POSTed back to the server, which
replies with raw JavaScript that patches the live page - there is no
clean data endpoint to call, so this module drives the same protocol a
browser does.

Confirmed live (via two captured HARs of real sessions against a RAy
answering as a backhaul radio, admin level): login, and running the
page's own built-in CLI tool (Tools > CLI) - `cli_help`, `cli_info_link`,
`cli_cnf_show` and `cli_info_station` were all exercised and their
exact output captured; `get_config()`/`get_system_info()` parse the
latter two into a dict/dataclass. `cli_help` itself lists the full
command catalogue (see KNOWN_CLI_COMMANDS below) - the rest of it is
the device's own documentation of what exists, not yet independently
exercised by this module.

Protocol, confirmed live from the HAR:
- GET / (unauthenticated) returns the bootstrap HTML. Two things in it
  matter: `var w='?wtd=XXXXX'+"&sid="+NNNNNNNNNN;` (the session token
  and an integer seed for the ack-id sequence below), and the login
  button's own event id, embedded as a plain HTML attribute on first
  load - `<button id="login_button" name="signal=XXXXX" ...>` - unlike
  every later page, which only ever expresses an element's event id
  inside an onclick handler (see _find_onclick_signal()).
- Every subsequent call is `POST /?wtd=<session>` with a
  `request=jsupdate` form body. Two request shapes are used:
    - `signal=poll` - a no-op heartbeat; the response is empty
      (Content-Length 0) until something server-side actually changed.
    - `signal=<event-id>` plus whatever form fields that widget's event
      handler captured (mouse position, the clicked element's own
      id as `tid`, and - for a form's own fields - their current
      values, e.g. `cli__command`/`cli__screen`) - this is how a
      "click" is simulated.
  Every response (including empty ones) starts `Wt._p_.response(N);` -
  N becomes the next request's `ackId`. The very first request's
  `ackId` is `sid + 1` (the seed read out of the bootstrap page).
  A response occasionally also calls `Wt._p_.setSessionUrl('?wtd=
  YYYYY')` - when seen, every later request must switch to that new
  wtd value (confirmed live: the session token itself changes
  immediately after login).
- An element's event id ("signal") is NOT stable across sessions - it's
  regenerated on every page load (confirmed live: the login button's
  own id was different across three separate unauthenticated loads of
  the same device). It always has to be read fresh out of whatever
  page/response last rendered that element, never hardcoded or cached
  across a new login.
- Navigating to a different page/tool happens exactly like any other
  click - the left-hand menu's anchors have stable, human-readable ids
  (`menuanchor__cli`, `menuanchor__graphs`, `menuanchor__detailed`, ...)
  rather than the short opaque ids used for most other widgets, which
  makes them the one thing in this protocol safe to hardcode. The CLI
  and Ping tools, though, are confirmed live to sit *inside* a
  collapsed top-level section ("Programs") - `menuanchor__cli` isn't
  actually present in the DOM at all until `menuanchor__programs`
  itself has been clicked once to reveal it. _reveal() walks a list of
  anchor ids for exactly this reason, skipping any already visible
  rather than assuming a fixed click depth - confirmed live that
  expanding Programs alone was already enough to reach the Ping tool's
  own send button, with nothing suggesting a further "Tools" click was
  needed in between.
- The CLI tool's own "Run" button (`cli__run__btn`) is a click like any
  other; the command name goes in the `cli__command` field already
  present on that page (the GUI actually uses a second, custom-command
  field - `cli__custom_commands` - confirmed always empty in both
  captures; command selection happens by setting `cli__command`
  directly). The result doesn't come back on the click's own response -
  that just echoes the command back (e.g. `cli__screen` set to
  '>> cli_info_station\\n' alone) and disables `cli__run__btn` /
  enables `cli__stop__btn` - confirmed live that a slower command's
  real output can itself still be incomplete on an intervening poll
  (`cli_info_station`'s own output was cut off mid-string on its first
  poll), so an output's mere presence is never enough to trust it as
  final. The one reliable completion signal is `run_btn_id` itself
  being re-enabled - confirmed live as the literal, non-string-nested
  text `$('#cli__run__btn').removeClass('Wt-disabled')` - only a poll
  carrying that marker has _run_tool() trust `cli__screen`'s value in
  that same response as the finished result. Confirmed live too: a
  command's own output never accumulates with a previous command's -
  `cli_cnf_show` finished in a single poll, `cli_info_station` right
  after it took two, and its final value was its own output alone, not
  `cli_cnf_show`'s output plus its own.

Not confirmed live, inferred by (strong) analogy:
- ping() - neither capture's window included the actual
  `menuanchor__ping` click, only the identical click-then-poll-for-
  `ping__screen` cycle this module reuses for it (confirmed live for
  CLI). The anchor id is a guess, consistent with the naming of every
  other confirmed menu anchor.
- Every CLI command beyond `cli_help`/`cli_info_link`/`cli_cnf_show`/
  `cli_info_station` in KNOWN_CLI_COMMANDS - these are the device's own
  listing of what exists (verbatim from a live `cli_help` call), not
  independently verified output shapes. run_cli() will happily run any
  of them; this module just doesn't parse their output into anything
  structured yet.

Sensitive data warning: `cli_cnf_show` is confirmed live to return
real secrets in plain text - this author's own capture included a live
SNMP community string and a USB-WiFi passphrase - never log/print its
output, or get_config()'s parsed dict, wholesale.
'''
from __future__ import annotations

# System imports
import dataclasses
import logging
import re
import time

# External imports
import requests

# Local imports
from . import exceptions

logger = logging.getLogger(__name__)

# The RAy's own page <title> - present, unauthenticated, on every page
# this firmware serves (confirmed live) - the one pre-auth signal this
# protocol offers to tell a RAy apart from anything else before ever
# trying a password.
TITLE_MARKER = 'RACOM RAy - microwave bridge configuration'

# Verbatim from a live cli_help call (see module docstring) - exists to
# tell callers what run_cli() can be pointed at, not as a verified
# contract for any command's output shape.
KNOWN_CLI_COMMANDS = {
    'cli_help': 'basic info about CLI commands',
    'cli_cnf_backup_get': 'create configuration backup package',
    'cli_cnf_factory_set': 'return to factory settings',
    'cli_cnf_set': 'update configuration',
    'cli_cnf_show': 'show configuration',
    'cli_time_set': 'change time',
    'cli_timezone_set': 'change time',
    'cli_rcinfo_list': 'show list of stored and active rcinfo files',
    'cli_rcinfo_check': 'check rcinfo from storage',
    'cli_rcinfo_set': 'select rcinfo from storage as active',
    'cli_fkey_list': 'show list of feature keys',
    'cli_fkey_add': 'load feature key',
    'cli_fkey_remove': 'remove feature key',
    'cli_fkey_clear': 'clear all feature keys',
    'cli_info_fail_safe': 'show fail safe status',
    'cli_info_link': 'show peer link status',
    'cli_info_log': 'show selected log',
    'cli_info_radio': 'show actual radio info',
    'cli_info_station': 'show station info',
    'cli_info_usb': 'show info about usb device',
    'cli_stat_clear': 'clear statistic',
    'cli_stat_show': 'show statistic',
    'cli_alarm_ack': 'acknowledge',
    'cli_alarm_show': 'show alarms last occurance',
    'cli_bandindex_set': 'set band index',
    'cli_unit_modeS_set': 'set mode S',
    'cli_fw_buffer_status': 'show status of FW buffer',
    'cli_fw_clear_buffer': 'clear FW buffer',
    'cli_fw_load_package': 'load FW package to buffer',
    'cli_fw_upgrade': 'upgrade FW from buffer',
    'cli_fw_upload2peer': 'upload FW from local buffer to peer',
    'cli_user_add': 'add new user',
    'cli_user_authkey': 'manage authorized keys of current user',
    'cli_user_authkey_any': 'manage authorized keys of any user',
    'cli_user_backup': 'create backup of user settings',
    'cli_user_mirror': 'copy user settings to peer',
    'cli_user_passwd': 'set password of current user',
    'cli_user_passwd_any': 'set password of any user',
    'cli_user_remove': 'remove user',
    'cli_user_repair_home': 'repair home directory',
    'cli_user_restore': 'restore user settings from backup file',
    'cli_user_show': 'show user information',
    'cli_user_showall': 'list all users',
    'cli_link_key_apply': 'set keys from buffer as stored secure keys',
    'cli_link_key_clear': 'clear peer link key buffer',
    'cli_link_key_gen': 'generate a new peer link key',
}

# Matches e.g. "var w='?wtd=XXXXX'+\"&sid=\"+123456789;" in the
# bootstrap page - the session token and the ack-id sequence's seed.
_BOOTSTRAP_RE = re.compile(r"wtd=([A-Za-z0-9_-]+)'\+\"&sid=\"\+(\d+)")
# The login button's event id is a plain HTML attribute only on this
# first, unauthenticated render - see module docstring.
_LOGIN_SIGNAL_RE = re.compile(r'id="login_button"\s+name="signal=(\w+)"')
# Every later page expresses an element's event id as the second
# argument of its own onclick handler's Wt._p_.update() call.
_ONCLICK_SIGNAL_TEMPLATE = r'id="{}"[^>]*onclick="[^"]*Wt\._p_\.update\(o,\\?\'(\w+)\\?\''
_RESPONSE_ACK_RE = re.compile(r'^Wt\._p_\.response\((\d+)\);')
_SESSION_URL_RE = re.compile(r"Wt\._p_\.setSessionUrl\('([^']*wtd=([^&']+)[^']*)'\)")
_LOGIN_FORM_PRESENT_RE = re.compile(r'id="login_username"')


def _unescape_js_string(raw: str) -> str:
    '''Unescape a single-quoted JS string literal body (the bytes
    between the quotes, not including them) - handles exactly the
    escapes confirmed live in a RAy's own responses: \\\\, \\', \\",
    \\n, \\r, \\t. Falls back to leaving any other backslash escape
    as-is rather than guessing at JS escape rules this module hasn't
    seen a real example of.
    '''
    out = []
    i = 0
    simple = {'\\': '\\', "'": "'", '"': '"', 'n': '\n', 'r': '\r', 't': '\t'}
    while i < len(raw):
        char = raw[i]
        if char == '\\' and i + 1 < len(raw) and raw[i + 1] in simple:
            out.append(simple[raw[i + 1]])
            i += 2
        else:
            out.append(char)
            i += 1
    return ''.join(out)


# Matches one cli_cnf_show line - confirmed live shape: shell-style
# KEY="value" (string) or KEY=value (bare int/decimal/keyword), one
# setting per line, under `#`-prefixed section-header comments.
_CNF_LINE_RE = re.compile(r'^([A-Z][A-Z0-9_]*)=(?:"([^"]*)"|(\S*))$')


def parse_cli_cnf_show(raw: str) -> dict[str, str]:
    '''Parse cli_cnf_show's own output (see Ray3Device.get_config())
    into a flat {key: value} dict.

    Every value is kept as a plain string - the dump mixes quoted
    strings, bare integers (e.g. RADIO_TX_CHAN=24190000) and bare
    decimals (e.g. RADIO_ANT_GAIN=10.00) with no reliable way to tell a
    deliberately-string value from a numeric-looking one just by
    format, so this doesn't guess at per-key types - cast whichever
    specific keys you need yourself.

    Sensitive data warning: the real output this was confirmed against
    includes SNMP_COMMUNITY_STRING and USB_WIFI_PASSPHRASE in plain
    text - don't log/print the result wholesale either.
    '''
    config = {}
    for line in raw.splitlines():
        match = _CNF_LINE_RE.match(line.strip())
        if match is None:
            continue
        key, quoted, bare = match.groups()
        config[key] = quoted if quoted is not None else bare
    return config


@dataclasses.dataclass
class Ray3StationInfo:
    '''Structured view of cli_info_station's own output (see
    Ray3Device.get_system_info()).'''
    product_code: str
    serial_number: str
    hw_type: str
    band_index: str
    mode_s: str
    radio_sw_version: str
    firmware_version: str
    firmware_name: str
    tx_polarization: str
    mac_address: str
    time: str


# cli_info_station's own label -> Ray3StationInfo field name.
_INFO_STATION_FIELD_MAP = {
    'Product code': 'product_code',
    'Serial number': 'serial_number',
    'HW type': 'hw_type',
    'Band index': 'band_index',
    'Mode S': 'mode_s',
    'Radio SW version': 'radio_sw_version',
    'Firmware version': 'firmware_version',
    'Firmware name': 'firmware_name',
    'TX polarization': 'tx_polarization',
    # Confirmed live: the device's own output really does spell this
    # "adress" - matched verbatim, not a typo to "fix" here.
    'MAC adress': 'mac_address',
    'Time': 'time',
}


def parse_cli_info_station(raw: str) -> Ray3StationInfo:
    '''Parse cli_info_station's own output (confirmed live shape: one
    "Label: value" line per field) into a Ray3StationInfo. A label this
    module doesn't recognize is ignored rather than raising - this
    output's exact field set may vary by firmware version, and nothing
    here needs it to be exhaustive, only the fields _INFO_STATION_
    FIELD_MAP already knows how to name.
    '''
    values = {}
    for line in raw.splitlines():
        label, sep, value = line.partition(':')
        if not sep:
            continue
        field = _INFO_STATION_FIELD_MAP.get(label.strip())
        if field:
            values[field] = value.strip()

    return Ray3StationInfo(**{
        field: values.get(field, '') for field in Ray3StationInfo.__dataclass_fields__
    })


def is_ray3_device(management_ip: str, timeout: int = 10, use_ssl: bool = False) -> bool:
    '''Pre-auth identification - True if `management_ip` answers like a
    RAy (its page <title> is confirmed live to carry TITLE_MARKER on
    every page, logged in or not). No login attempted.

    Returns False (never raises) on any connection problem, same
    "inconclusive means False, not an exception" contract as a
    determine_device_type()-style probe - a caller sweeping a lot of
    IPs shouldn't have to wrap this in its own try/except.
    '''
    scheme = 'https' if use_ssl else 'http'
    try:
        res = requests.get(f"{scheme}://{management_ip}/", timeout=timeout, verify=False)
    except requests.exceptions.RequestException:
        return False
    return res.status_code == 200 and TITLE_MARKER in res.text


class Ray3Device:
    '''RACOM RAy device handler (RAy2/RAy3 - both run this same Wt-based
    GUI; nothing observed so far distinguishes them at the protocol
    level).

    Read-only for now: login and run_cli() (plus ping(), inferred by
    analogy - see module docstring) are the only confirmed-or-close
    primitives. change_password()/apply_changes() are deliberate
    no-ops, same discipline as this author's other packages - cli_user_
    passwd/cli_cnf_set exist per the device's own cli_help, but neither
    has been exercised live yet.
    '''

    _default_timeout = 30
    _verify_ssl = False
    _poll_interval_s = 0.5
    _poll_timeout_s = 15

    def __init__(
        self, management_ip: str, timeout: int | None = None, use_ssl: bool = False,
    ) -> None:
        '''Constructor.

        Args:
            management_ip: Hostname or IP of the device.
            timeout: Per-request timeout in seconds. Defaults to 30.
            use_ssl: Use https instead of http. Defaults to False - the
                captured session used plain http (the login page itself
                offers a link to "go to secured version", implying http
                is the normal/default case, not just what this one unit
                happened to be configured for).
        '''
        self._mgmt_ip = management_ip
        self._timeout = self._default_timeout if timeout is None else timeout
        self._use_ssl = use_ssl

        self._curr_username = None
        self._curr_password = None
        self._session = requests.Session()

        self._wtd = None
        self._ack = None
        self._page_id = 1
        self._last_page = ''
        self._revealed = set()

    def _base_url(self) -> str:
        scheme = 'https' if self._use_ssl else 'http'
        return f"{scheme}://{self._mgmt_ip}/"

    def _bootstrap(self) -> str:
        '''GET the unauthenticated root page and capture the session
        token, the ack-id sequence's seed, and this load's login button
        event id. Returns the raw page body (callers that need the
        login signal read it via _LOGIN_SIGNAL_RE themselves).

        Raises:
            exceptions.DeviceUnavailable: the device didn't respond.
        '''
        try:
            res = self._session.get(
                self._base_url(), timeout=self._timeout, verify=self._verify_ssl,
            )
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout) as exc:
            raise exceptions.DeviceUnavailable from exc

        match = _BOOTSTRAP_RE.search(res.text)
        if match is None:
            raise exceptions.DeviceUnavailable(
                f"Could not find a Wt session token in {self._mgmt_ip}'s root page - "
                "doesn't look like a RAy"
            )
        self._wtd = match.group(1)
        self._ack = int(match.group(2)) + 1
        return res.text

    def _post_update(self, signal: str, extra_fields: dict | None = None) -> str:
        '''One request=jsupdate round trip. Updates the tracked ackId
        (and, if the response changed it, the session token) before
        returning the raw response body.

        Raises:
            exceptions.DeviceUnavailable: the device didn't respond, or
                a response arrived with no ackId - not a shape this
                protocol has ever been confirmed to return, so treated
                as a connectivity failure rather than silently
                continuing with a stale ackId.
        '''
        body = {
            'request': 'jsupdate',
            'signal': signal,
            'ackId': str(self._ack),
            'pageId': str(self._page_id),
            **(extra_fields or {}),
        }
        try:
            res = self._session.post(
                f"{self._base_url()}?wtd={self._wtd}",
                data=body,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout) as exc:
            raise exceptions.DeviceUnavailable from exc

        text = res.text
        if not text:
            return text

        ack_match = _RESPONSE_ACK_RE.match(text)
        if ack_match is None:
            raise exceptions.DeviceUnavailable(
                f"Unexpected response from {self._mgmt_ip} (no ackId) - session may have expired"
            )
        self._ack = int(ack_match.group(1))

        session_match = _SESSION_URL_RE.search(text)
        if session_match is not None:
            self._wtd = session_match.group(2)

        return text

    @staticmethod
    def _find_onclick_signal(body: str, element_id: str) -> str | None:
        '''Find `element_id`'s event id in a post-login (widget-diff
        style) response - see module docstring. None if that element
        isn't present in this particular response at all.
        '''
        pattern = re.compile(_ONCLICK_SIGNAL_TEMPLATE.format(re.escape(element_id)))
        match = pattern.search(body)
        return match.group(1) if match else None

    def login_http(self, curr_pw: str, curr_user: str | None = None) -> None:
        '''Login to the device via its web GUI.

        Args:
            curr_pw: Password to use for login.
            curr_user: Username to use for login. Required - unlike
                this author's other packages, no default has been
                confirmed live for a RAy (the login page's username
                field is blank, not pre-filled).

        Raises:
            exceptions.WrongPassword: the login was rejected. Detection
                is inferred, not confirmed live (the captured session
                only ever logged in successfully) - a response that
                still contains the login form's own username field is
                treated as a failed attempt.
            exceptions.DeviceUnavailable: the device didn't respond, or
                didn't look like a RAy at all.
        '''
        if curr_user is None:
            raise ValueError("RAy3Device has no default username - one must be given")

        bootstrap_body = self._bootstrap()
        if TITLE_MARKER not in bootstrap_body:
            raise exceptions.DeviceUnavailable(
                f"{self._mgmt_ip} doesn't look like a RAy (missing {TITLE_MARKER!r} in its page)"
            )

        login_signal_match = _LOGIN_SIGNAL_RE.search(bootstrap_body)
        if login_signal_match is None:
            raise exceptions.DeviceUnavailable(
                f"Could not find the login button's event id on {self._mgmt_ip}'s root page"
            )

        # The initial `signal=load` call the bootstrap JS itself fires
        # before any user interaction - confirmed live as the very
        # first jsupdate of every captured session.
        self._post_update('load', {'login_password': '', 'login_username': '', 'focus': ''})

        response = self._post_update(
            login_signal_match.group(1),
            {
                'login_username': curr_user,
                'login_password': curr_pw,
                'focus': 'login_button',
                'tid': 'login_button',
                'type': 'click',
                'clientX': '0', 'clientY': '0', 'documentX': '0', 'documentY': '0',
                'dragdX': '0', 'dragdY': '0', 'wheel': '-1',
                'screenX': '0', 'screenY': '0', 'scrollX': '0', 'scrollY': '0',
                'width': '45', 'height': '19', 'widgetX': '0', 'widgetY': '0',
                'button': '1', 'charCode': '0',
            },
        )

        if _LOGIN_FORM_PRESENT_RE.search(response):
            raise exceptions.WrongPassword(f"Login to RAy '{self._mgmt_ip}' failed")

        self._curr_username = curr_user
        self._curr_password = curr_pw
        # The post-login page (confirmed live: a single ~19KB response)
        # already carries the top-level menu, including the
        # 'menuanchor__programs' entry _reveal() starts from - see its
        # own docstring for why navigation starts from here rather than
        # re-fetching anything.
        self._last_page = response

    def login(
        self, passwords: list[str], username: str, auto_apply: bool = False,
    ) -> None:
        '''Login to the device, trying `passwords` in order.

        Mirrors this author's other packages' login() - on success with
        anything but passwords[0], change_password(passwords[0]) is
        called (currently a no-op here, see its docstring).
        '''
        self._curr_username = username

        try:
            primary_pw = passwords[0]
            self.login_http(curr_pw=primary_pw, curr_user=username)
        except exceptions.WrongPassword as exc:
            logger.debug("Primary password failed, trying alternates")
            pw_found = False
            for curr_pw in passwords[1:]:
                try:
                    self.login_http(curr_pw=curr_pw, curr_user=username)
                    self.change_password(primary_pw)
                    if auto_apply:
                        self.apply_changes()
                    pw_found = True
                    break
                except exceptions.WrongPassword:
                    pass

            if not pw_found:
                raise exceptions.WrongPassword(
                    f"Device {self._mgmt_ip} does not have a known password"
                ) from exc

    def change_password(self, new_password: str) -> None:
        '''No-op for now - fetch-only, never modifies the device.'''
        logger.debug(
            f"change_password() called on {self._mgmt_ip} but is a no-op - not implemented yet"
        )

    def apply_changes(self, test_mode: bool = False) -> None:
        '''No-op for now - fetch-only, never modifies the device.'''
        logger.debug(
            f"apply_changes() called on {self._mgmt_ip} but is a no-op - not implemented yet"
        )

    def _reveal(self, anchor_ids: list[str]) -> str:
        '''Click through a chain of left-hand-menu anchors, each one
        only if its event id isn't already visible in what's been
        rendered so far - returns the accumulated page body once the
        chain is exhausted.

        Confirmed live: the post-login page already contains
        'menuanchor__programs' (a stable, human-readable id, like every
        other top-level menu anchor - see module docstring), but its
        own children (including 'menuanchor__cli'/'menuanchor__ping')
        are only added to the DOM once it's actually clicked - so a
        caller after 'menuanchor__cli' has to pass
        ['menuanchor__programs', 'menuanchor__cli'], not just the
        latter. Skipping an id already visible (rather than always
        clicking every one in the chain) tolerates this device
        revealing more of the tree in one click than strictly
        documented - confirmed live that clicking 'menuanchor__programs'
        alone was enough to make the Ping tool's own send button appear,
        with no separate click for a 'Tools' sub-level in between.

        Only ever clicks through once per `anchor_ids[-1]` for the life
        of this object (tracked in `self._revealed`) - re-checking a
        menu item's own presence in whatever the most recent response
        happened to contain isn't reliable once already revealed (Wt
        only ever sends *changes*, so a later poll/click response has
        no reason to repeat unchanged menu markup), and a top-level
        item like 'menuanchor__programs' is normally an expand/collapse
        toggle - re-clicking an already-expanded one could collapse it
        right back.

        Raises:
            exceptions.DeviceUnavailable: one of `anchor_ids` never
                appeared, even after clicking everything before it -
                not logged in, or this device/firmware's menu tree
                doesn't have it.
        '''
        target = anchor_ids[-1]
        if target in self._revealed:
            return self._last_page

        body = self._last_page
        for anchor_id in anchor_ids:
            if f'id="{target}"' in body:
                break
            signal = self._find_onclick_signal(body, anchor_id)
            if signal is None:
                raise exceptions.DeviceUnavailable(
                    f"Could not find menu anchor '{anchor_id}' - not logged in, or this "
                    "device/firmware doesn't have it"
                )
            body = self._post_update(signal, {'tid': anchor_id, 'type': 'click'})
            self._last_page = body

        self._revealed.update(anchor_ids)
        return body

    def _run_tool(
        self, page_body: str, run_btn_id: str, screen_field: str, fields: dict,
    ) -> str:
        '''Shared click-then-poll-for-result cycle behind run_cli() and
        ping() - see module docstring's Protocol section.

        Confirmed live (second capture, running cli_cnf_show then
        cli_info_station back to back): the click's own response
        already sets `screen_field` to just the echoed command (e.g.
        '>> cli_info_station\\n') before any real output exists, and a
        slower command's output can itself arrive incomplete across
        more than one poll, still being written - confirmed live
        ('cli_info_station's own output was still mid-string on its
        first poll, complete only on the next one that also re-enabled
        `run_btn_id`). So an output's own presence is never enough; the
        one reliable "this is the final value" signal is `run_btn_id`
        itself being re-enabled (`$('#<run_btn_id>').removeClass(
        'Wt-disabled')` - confirmed live, a plain string in the
        response, not nested in a JS string literal so needing no
        unescaping) - only a poll response carrying that marker has
        this call trust `screen_field`'s value as final. Also confirmed
        live: back-to-back commands' outputs don't accumulate - each
        one's final value is that command's own output alone, not the
        previous command's output plus the new one.

        Args:
            page_body: the page content the tool's run/send button
                should already be visible in (typically _reveal()'s
                return value).
            run_btn_id: the tool's "Run"/"Send" button element id.
            screen_field: the textarea/field name the result appears in
                (e.g. 'cli__screen', 'ping__screen').
            fields: the tool's own form fields for this run (e.g.
                {'cli__command': 'cli_info_link'}).

        Raises:
            exceptions.DeviceUnavailable: `run_btn_id` wasn't in
                `page_body` at all.
            exceptions.CommandTimeout: no result seen within
                _poll_timeout_s.
        '''
        signal = self._find_onclick_signal(page_body, run_btn_id)
        if signal is None:
            raise exceptions.DeviceUnavailable(
                f"Could not find '{run_btn_id}' - wrong page loaded?"
            )

        done_marker = f"$('#{run_btn_id}').removeClass('Wt-disabled')"
        result_re = re.compile(
            r"\$\('" + re.escape(screen_field) + r"'\);\s*\n?\s*\w+\.value='((?:[^'\\]|\\.)*)';"
        )

        response = self._post_update(signal, {
            **fields,
            screen_field: '',
            'focus': run_btn_id,
            'tid': run_btn_id,
            'type': 'click',
            'clientX': '0', 'clientY': '0', 'documentX': '0', 'documentY': '0',
            'dragdX': '0', 'dragdY': '0', 'wheel': '-1',
            'screenX': '0', 'screenY': '0', 'scrollX': '0', 'scrollY': '0',
            'width': '54', 'height': '16', 'widgetX': '0', 'widgetY': '0',
            'button': '1', 'charCode': '0',
        })

        deadline = time.monotonic() + self._poll_timeout_s
        while time.monotonic() < deadline:
            if done_marker in response:
                match = result_re.search(response)
                if match is not None:
                    return _unescape_js_string(match.group(1))
            response = self._post_update('poll')
            time.sleep(self._poll_interval_s)

        raise exceptions.CommandTimeout(
            f"No result from '{run_btn_id}' within {self._poll_timeout_s}s"
        )

    def run_cli(self, command: str) -> str:
        '''Run one command via the device's own Tools > CLI page and
        return its output, verbatim - confirmed live for `cli_help`,
        `cli_info_link`, `cli_cnf_show` and `cli_info_station`; see
        KNOWN_CLI_COMMANDS for the device's own full catalogue, and the
        module docstring's sensitive-data warning before calling
        `cli_cnf_show`/`cli_cnf_backup_get` (confirmed live: cli_cnf_show
        really does include live secrets - the SNMP community string,
        a USB-WiFi passphrase - never log/print its output wholesale).

        Args:
            command: One of KNOWN_CLI_COMMANDS' keys, or any other
                command name this device's own cli_help lists that
                isn't in that (possibly incomplete) catalogue.

        Raises:
            exceptions.CommandTimeout: no result within the poll
                timeout.
            exceptions.DeviceUnavailable: the CLI page/button couldn't
                be found - not logged in, or no Tools > CLI page on
                this device/firmware.
        '''
        page_body = self._reveal(['menuanchor__programs', 'menuanchor__cli'])
        return self._run_tool(
            page_body, 'cli__run__btn', 'cli__screen',
            {'cli__cli_commands': '0', 'cli__command': command, 'cli__custom_commands': ''},
        )

    def get_config(self) -> dict[str, str]:
        '''This device's full configuration (run_cli('cli_cnf_show'),
        parsed) - confirmed live, including real station identification
        (SVC_STATION_NAME/SVC_STATION_LOCATION) and IP addressing
        (SVC_IP/SVC_MASK_LEN/SVC_GW).

        Sensitive data warning: see module docstring - the parsed dict
        includes real secrets (SNMP_COMMUNITY_STRING, USB_WIFI_
        PASSPHRASE confirmed live), never log/print it wholesale.

        Raises:
            exceptions.CommandTimeout: no result within the poll
                timeout.
            exceptions.DeviceUnavailable: the CLI page/button couldn't
                be found.
        '''
        return parse_cli_cnf_show(self.run_cli('cli_cnf_show'))

    def get_system_info(self) -> Ray3StationInfo:
        '''This device's own identification (run_cli('cli_info_station'),
        parsed) - confirmed live.

        Raises:
            exceptions.CommandTimeout: no result within the poll
                timeout.
            exceptions.DeviceUnavailable: the CLI page/button couldn't
                be found.
        '''
        return parse_cli_info_station(self.run_cli('cli_info_station'))

    def ping(self, destination: str, count: int = 5, size: int = 56) -> str:
        '''Run a ping from the device itself via its Tools > Ping page
        and return the raw output.

        NOT confirmed live the same way run_cli() is - see module
        docstring. The click-then-poll mechanism, and 'menuanchor__
        programs' itself, are confirmed (the captured session's ping
        use happened straight after expanding Programs); only the
        'menuanchor__ping' id itself is an inferred guess, consistent
        with the naming of every confirmed menu anchor.

        Args:
            destination: Hostname or IP to ping.
            count: Number of packets to send. Defaults to 5.
            size: Packet size in bytes. Defaults to 56.

        Raises:
            exceptions.CommandTimeout: no result within the poll
                timeout.
            exceptions.DeviceUnavailable: the Ping page/button couldn't
                be found.
        '''
        page_body = self._reveal(['menuanchor__programs', 'menuanchor__ping'])
        return self._run_tool(
            page_body, 'ping__send__btn', 'ping__screen',
            {
                'ping__destination': destination,
                'ping__count': str(count),
                'ping__size_by': str(size),
            },
        )

    def logout(self) -> None:
        '''Log out of the device - confirmed live (the captured session
        ends with exactly this click).'''
        signal = self._find_onclick_signal(self._last_page, 'logout_button')
        if signal is None:
            return
        self._post_update(
            signal, {'focus': 'logout_button', 'tid': 'logout_button', 'type': 'click'},
        )
