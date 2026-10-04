'''Tests for Ray3Device - covers the Wt session bootstrap (wtd/ack-id
extraction), the login click, and the CLI run-then-poll cycle. Every
mocked response body is fabricated to match the *shape* device.py's
module docstring documents as confirmed live, not real captured data.
'''
import pytest
import requests

from ray3_automata import exceptions
from ray3_automata.device import Ray3Device, is_ray3_device

from .conftest import bootstrap_page, response_ack

BASE = 'http://192.0.2.1/'


class TestIsRay3Device:
    def test_true_when_title_present(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page())
        assert is_ray3_device('192.0.2.1') is True

    def test_false_when_title_missing(self, requests_mock):
        requests_mock.get(BASE, text='<html><title>Something Else</title></html>')
        assert is_ray3_device('192.0.2.1') is False

    def test_false_on_connection_error(self, requests_mock):
        requests_mock.get(BASE, exc=requests.exceptions.ConnectionError)
        assert is_ray3_device('192.0.2.1') is False


class TestLoginHttp:
    def test_success_stores_credentials_and_page(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page(wtd='SESSION1', sid=1000))
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(1002)},  # the bootstrap 'load' call
                {'text': response_ack(1003, '<a id="logout_button" '
                    'onclick="Wt._p_.update(o,\'sLOGOUT1\',e,true);">Logout</a>')},
            ],
        )
        dev = Ray3Device('192.0.2.1')

        dev.login_http('correct-password', 'admin')

        assert dev._curr_username == 'admin'
        assert dev._curr_password == 'correct-password'
        assert 'logout_button' in dev._last_page

    def test_wrong_password_raises(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page())
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(2)},
                # Still carries the login form -> treated as rejected.
                {'text': response_ack(3, '<input id="login_username"/>')},
            ],
        )
        dev = Ray3Device('192.0.2.1')

        with pytest.raises(exceptions.WrongPassword):
            dev.login_http('wrong-password', 'admin')

    def test_connection_error_is_device_unavailable(self, requests_mock):
        requests_mock.get(BASE, exc=requests.exceptions.ConnectionError)
        dev = Ray3Device('192.0.2.1')

        with pytest.raises(exceptions.DeviceUnavailable):
            dev.login_http('correct-password', 'admin')

    def test_non_ray_device_is_device_unavailable(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page().replace('RACOM RAy', 'Something Else'))
        dev = Ray3Device('192.0.2.1')

        with pytest.raises(exceptions.DeviceUnavailable):
            dev.login_http('correct-password', 'admin')


class TestLogin:
    def test_falls_back_to_alternate_password(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page())
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(2)},  # load, 1st attempt
                {'text': response_ack(3, '<input id="login_username"/>')},  # rejected
                {'text': response_ack(4)},  # load, 2nd attempt
                {'text': response_ack(5, '<a id="logout_button" '
                    'onclick="Wt._p_.update(o,\'sLOGOUT1\',e,true);">Logout</a>')},
            ],
        )
        dev = Ray3Device('192.0.2.1')

        dev.login(['current-password', 'old-password'], username='admin')

        assert dev._curr_password == 'old-password'

    def test_no_known_password_raises(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page())
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(2)},
                {'text': response_ack(3, '<input id="login_username"/>')},
            ],
        )
        dev = Ray3Device('192.0.2.1')

        with pytest.raises(exceptions.WrongPassword):
            dev.login(['only-guess'], username='admin')


def _programs_expanded_page() -> str:
    '''The response to expanding 'menuanchor__programs' - carries the
    CLI/Ping tool pages' own signals, per device.py's _reveal() design
    (see its docstring for why a single expand can surface more than
    one tool page at once).'''
    return (
        '<a id="menuanchor__cli" onclick="Wt._p_.update(o,\'sMENUCLI\',e,true);">CLI</a>'
        '<a id="menuanchor__ping" onclick="Wt._p_.update(o,\'sMENUPING\',e,true);">Ping</a>'
        '<button id="cli__run__btn" '
        'onclick="Wt._p_.update(o,\'sCLIRUN1\',e,true);">Run</button>'
        '<textarea id="cli__screen"></textarea>'
        '<button id="ping__send__btn" '
        'onclick="Wt._p_.update(o,\'sPINGRUN1\',e,true);">Send</button>'
        '<textarea id="ping__screen"></textarea>'
    )


class TestRunCli:
    def _logged_in_device(self, requests_mock):
        requests_mock.get(BASE, text=bootstrap_page(wtd='SESSION1', sid=1000))
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(1002)},
                {'text': response_ack(1003, '<a id="menuanchor__programs" '
                    'onclick="Wt._p_.update(o,\'sPROGRAMS1\',e,true);">Programs</a>')},
            ],
        )
        dev = Ray3Device('192.0.2.1')
        dev.login_http('correct-password', 'admin')
        return dev

    def test_runs_command_and_returns_output(self, requests_mock):
        dev = self._logged_in_device(requests_mock)
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(1004, _programs_expanded_page())},  # expand Programs
                {'text': response_ack(1005)},  # click Run - canvas-redraw noise, no result yet
                {'text': response_ack(1006)},  # poll - still nothing
                {'text': response_ack(1007, "var j1=Wt4_4_0.$('cli__screen');\n"
                    "j1.value='>> cli_info_link\\ncli_info_link: Link status: up\\n\\n"
                    "RETURNED VALUE: 0\\n';")},  # poll - result lands
            ],
        )

        output = dev.run_cli('cli_info_link')

        assert output == '>> cli_info_link\ncli_info_link: Link status: up\n\nRETURNED VALUE: 0\n'

    def test_second_call_only_returns_new_output(self, requests_mock):
        dev = self._logged_in_device(requests_mock)
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(1004, _programs_expanded_page())},
                {'text': response_ack(1005)},
                {'text': response_ack(1006, "var j1=Wt4_4_0.$('cli__screen');\n"
                    "j1.value='>> first\\n';")},
                {'text': response_ack(1007)},  # 2nd call's click
                {'text': response_ack(1008, "var j2=Wt4_4_0.$('cli__screen');\n"
                    "j2.value='>> first\\n>> second\\n';")},
            ],
        )

        first = dev.run_cli('cmd_one')
        second = dev.run_cli('cmd_two')

        assert first == '>> first\n'
        assert second == '>> second\n'

    def test_timeout_raises_command_timeout(self, requests_mock):
        dev = self._logged_in_device(requests_mock)
        dev._poll_timeout_s = 0.1
        dev._poll_interval_s = 0.05
        requests_mock.post(
            BASE,
            [
                {'text': response_ack(1004, _programs_expanded_page())},
                {'text': response_ack(1005)},
            ] + [{'text': response_ack(1006)}] * 10,  # polls, never a result
        )

        with pytest.raises(exceptions.CommandTimeout):
            dev.run_cli('cli_help')

    def test_missing_run_button_is_device_unavailable(self, requests_mock):
        dev = self._logged_in_device(requests_mock)
        requests_mock.post(
            BASE,
            [{'text': response_ack(1004, '<a id="menuanchor__cli">CLI, but no run button</a>')}],
        )

        with pytest.raises(exceptions.DeviceUnavailable):
            dev.run_cli('cli_help')
