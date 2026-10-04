'''Shared pytest fixtures and synthetic Wt-protocol response builders.

Every raw body below is fabricated to match the *shape* this package's
device.py docstring documents as confirmed live against a real device
(session tokens, element ids, JS escaping), but with made-up
tokens/ids rather than anything from a real captured session, since
this is a public repository.
'''
import pytest


def bootstrap_page(wtd: str = 'FAKEwtdTOKEN01', sid: int = 1000000000) -> str:
    '''A minimal unauthenticated root page - just enough to exercise
    _BOOTSTRAP_RE and _LOGIN_SIGNAL_RE, not a full real page.'''
    return (
        '<html><head><title>RACOM RAy - microwave bridge configuration</title>'
        '<script>var w=\'?wtd=' + wtd + '\'+"&sid="+' + str(sid) + ';</script></head>'
        '<body><form><input id="login_username" name="login_username"/>'
        '<input id="login_password" name="login_password"/>'
        '<button id="login_button" name="signal=sLOGIN1" type="submit">Log in</button>'
        '</form></body></html>'
    )


def response_ack(ack: int, body: str = '') -> str:
    '''Wrap `body` the way every real response is prefixed.'''
    return f'Wt._p_.response({ack});{body}'


@pytest.fixture
def bootstrap_body():
    return bootstrap_page()


@pytest.fixture
def post_login_page():
    '''A post-login page carrying the menu anchors and the CLI run
    button, onclick-style (see device.py's _ONCLICK_SIGNAL_TEMPLATE).'''
    return (
        '<li><a id="menuanchor__cli" href="/" '
        'onclick="var e=event;Wt._p_.update(o,\'sMENUCLI\',e,true);">CLI</a></li>'
        '<li><a id="menuanchor__ping" href="/" '
        'onclick="var e=event;Wt._p_.update(o,\'sMENUPING\',e,true);">Ping</a></li>'
        '<a id="logout_button" onclick="Wt._p_.update(o,\'sLOGOUT1\',e,true);">Logout</a>'
    )


@pytest.fixture
def cli_page():
    return (
        '<button id="cli__run__btn" '
        'onclick="var e=event;Wt._p_.update(o,\'sCLIRUN1\',e,true);">Run</button>'
    )
