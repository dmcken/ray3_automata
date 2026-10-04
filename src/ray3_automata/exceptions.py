'''General exceptions'''


class DeviceUnavailable(Exception):
    '''Exception to handle a device being unavailable'''
class WrongPassword(Exception):
    '''Exception to handle a wrong password'''
class CommandTimeout(Exception):
    '''Exception to handle a CLI command not finishing within the given
    poll timeout. The device may still complete it after this is
    raised - this only means the result wasn't seen in time.'''
