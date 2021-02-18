#!/usr/bin/python3

from pathlib import Path
import time
import logging
import os
import sys, json
import socket
from stem.control import Controller
import stem.socket
from typing import List, Optional, Dict, Any, Tuple, cast

log = logging.getLogger("tor-launcher")


class StemFDSocket(stem.socket.ControlSocket):
    def __init__(self, fd: int):
        super().__init__()
        self.fd = fd

    @property
    def path(self) -> str:
        """
        I don't think that's ever called, but let's implement it

        returns sth like socket:[12345678], which is not great
        """
        fname = "/proc/%d/fd/%d" % (os.getpid(), self.fd)
        return Path(fname).resolve().name

    def _make_socket(self):
        """
        We don't need to create a new socket: let's reuse the FD!
        """
        return socket.socket(fileno=self.fd)


def recover_fd_from_parent() -> tuple:
    fds = [int(fd) for fd in os.getenv("INHERIT_FD", "").split(",")]
    # fds[0] must be a socket to Tor Control Port
    # fds[1] must be a rw fd for settings file

    controller = None
    socket = StemFDSocket(fds[1])
    controller = Controller(socket)

    configfile = os.fdopen(fds[0], "r+")

    return (configfile, controller)


# PROXY_TYPES is a sequence of Tor options related to proxing.
#     Those exact values are also used by TorConnectionProxy
PROXY_TYPES = ("Socks4Proxy", "Socks5Proxy", "HTTPSProxy")
PROXY_AUTH_OPTIONS = {
    "HTTPSProxy": ["HTTPSProxyAuthenticator"],
    "Socks5Proxy": ["Socks5ProxyUsername", "Socks5ProxyPassword"],
}


class TorConnectionProxy:
    """configuration item for proxy configuration"""

    def __init__(
        self,
        address: str,
        port: int,
        proxy_type: str,
        auth: Optional[Tuple[str, str]] = None,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.proxy_type = proxy_type
        self.address = address
        self.port = port
        self.auth = auth
        if not enabled:
            return
        if proxy_type not in PROXY_TYPES:
            raise ValueError("Invalid proxy type: `%s`" % proxy_type)
        if proxy_type == "Socks4Proxy" and auth is not None:
            raise ValueError("Socks4Proxy cannot have authentication")

    @classmethod
    def noproxy(cls) -> "TorConnectionProxy":
        r = cls(address="", port=0, proxy_type="", enabled=False)
        return r

    @classmethod
    def from_obj(cls, obj: dict) -> "TorConnectionProxy":
        kwargs = dict(
            proxy_type=obj["proxy_type"], address=obj["address"], port=int(obj["port"])
        )
        auth: Tuple[Optional[str], Optional[str]] = (
            obj.get("username"),
            obj.get("password"),
        )
        if all(x is None for x in auth):
            kwargs["auth"] = None
        elif any(x is None for x in auth):
            raise ValueError(
                "Proxy configuration object username and password must all be set, or none at all"
            )
        else:
            kwargs["auth"] = auth
        proxy = cls(**kwargs)
        return proxy

    def to_dict(self):
        if not self.enabled:
            return None
        r = {"proxy_type": self.proxy_type, "address": self.address, "port": self.port}
        if self.auth is not None:
            r["username"], r["password"] = self.auth
        return r

    @classmethod
    def from_tor_value(
        cls, proxy_type: str, val: str, auth_values: Optional[List[str]] = None
    ) -> "TorConnectionProxy":
        address, port = val.split(":")
        auth: Optional[Tuple[str, str]] = None
        if auth_values is not None:
            if len(auth_values) == 1:
                auth = cast(Tuple[str, str], tuple(auth_values[0].split(":", 1)))
            elif len(auth_values) == 2:
                auth = cast(Tuple[str, str], tuple(auth_values))
            else:
                raise ValueError(
                    "auth_values must either be ['user:password'] "
                    " or ['user', 'password']"
                )

        obj = cls(address, int(port), proxy_type, auth=auth)
        return obj

    def to_tor_value_options(self) -> Dict[str, Optional[str]]:
        r: Dict[str, Optional[str]] = {}
        if self.enabled:
            r[self.proxy_type] = "%s:%d" % (self.address, self.port)
        for proxy_type in PROXY_TYPES:
            if self.enabled and proxy_type == self.proxy_type:
                continue
            r[proxy_type] = None
        for pt in PROXY_TYPES:
            for option in PROXY_AUTH_OPTIONS.get(pt, []):
                r[option] = None
        if self.enabled and self.auth is not None:
            if self.proxy_type == "HTTPSProxy":
                r["HTTPSProxyAuthenticator"] = "%s:%s" % self.auth
            else:
                r["Socks5ProxyUsername"], r["Socks5ProxyPassword"] = self.auth
        print("TorOpts", r)
        return r


class TorConnectionConfig:
    def __init__(
        self,
        bridges: list = [],
        proxy: TorConnectionProxy = TorConnectionProxy.noproxy(),
    ):
        self.bridges: List[str] = bridges
        self.proxy: TorConnectionProxy = proxy

    @classmethod
    def load_from_tor_stem(cls, stem_controller: Controller):
        bridges: List[str] = []
        if stem_controller.get_conf("UseBridges") != "0":
            bridges = stem_controller.get_conf("Bridge", multiple=True)

        for proxy_type in PROXY_TYPES:
            val = stem_controller.get_conf(proxy_type)
            if val is not None:
                auth = [
                    stem_controller.get_conf(opt)
                    for opt in PROXY_AUTH_OPTIONS[proxy_type]
                ]

                proxy = TorConnectionProxy.from_tor_value(
                    proxy_type, val, auth_values=auth
                )
                break
        else:
            proxy = TorConnectionProxy.noproxy()

        config = cls(bridges=bridges, proxy=proxy)

        return config

    @classmethod
    def load_from_dict(cls, obj):
        """this method is suitable to retrieve configuration from a JSON object"""
        config = cls()
        config.bridges = obj.get("bridges", [])
        proxy = obj.get("proxy", None)
        if proxy is not None:
            config.proxy = TorConnectionProxy.from_obj(proxy)
        else:
            config.proxy = TorConnectionProxy.noproxy()
        return config

    def to_dict(self) -> dict:
        return {
            "bridges": self.bridges,
            "proxy": self.proxy.to_dict() if self.proxy is not None else None,
        }

    def to_tor_conf(self) -> Dict[str, Any]:
        """
        returns a dict whose output fits to stem.control.Controller.set_options
        """
        r: Dict[str, Any] = {}
        r["UseBridges"] = "1" if self.bridges else "0"
        r["Bridge"] = self.bridges if self.bridges else None
        r.update(self.proxy.to_tor_value_options().items())
        return r


class TorLauncherUtils:
    def __init__(self, stem_controller: Controller, config_buf):
        """
        Arguments:
        stem_controller -- an already connected and authorized stem Controller
        config_buf -- an already open read-write buffer to the configuration file
        """
        self.stem_controller = stem_controller
        self.config_buf = config_buf
        self.tor_connection_config = None

    def load_conf(self):
        self.read_conf()
        if self.tor_connection_config is None:
            self.tor_connection_config = TorConnectionConfig.load_from_tor_stem(
                self.stem_controller
            )

    def save_conf(self):
        if self.tor_connection_config is None:
            return
        self.config_buf.seek(0, os.SEEK_SET)
        self.config_buf.write(
            json.dumps(self.tor_connection_config.to_dict(), indent=2)
        )
        self.config_buf.truncate()

    def read_conf(self):
        self.config_buf.seek(0, os.SEEK_END)
        size = self.config_buf.tell()
        if not size:
            log.debug("Empty config file")
            return
        self.config_buf.seek(0)
        try:
            obj = json.load(self.config_buf)
        except JSONDecodeError:
            log.warning("Invalid config file")
            return
        finally:
            self.config_buf.seek(0)
        self.tor_connection_config = TorConnectionConfig.load_from_dict(obj)

    def apply_conf(self):
        self.stem_controller.set_conf("DisableNetwork", "1")
        self.stem_controller.set_options(self.tor_connection_config.to_tor_conf())
        self.stem_controller.set_conf("DisableNetwork", "0")

    def tor_has_bootstrapped(self) -> bool:
        resp = self.stem_controller.get_info("status/bootstrap-phase")
        if resp is None:
            log.warn("No response from ControlPort")
            return False
        parts = resp.split(" ")
        if parts[0] != "NOTICE":  # it is WARN
            return False

        if int(parts[2].split("=")[1]) != 100:
            return False

        # shall we really do also those checks?
        resp = self.stem_controller.get_info("status/circuit-established")
        if resp is None:
            log.warn("No response from ControlPort")
            return False
        if not bool(int(resp)):
            return False

        return True


class TorLauncherUtils:
    def __init__(self):
        pass

    def is_network_up(self):
        """
        This method checks whether we have an IP on some network interface.

        It does NOT care if we're really connected to the Internet
        """

        # XXX: does it do the right thing? we should check!
        try:
            subprocess.check_call(["nm-online", "-xq"])
            return True
        except subprocess.CalledProcessError:
            return False

    def is_internet_up(self):
        """
        This is similar to is_network_up(), but also checks if we're really connected to the Internet
        """
        raise NotImplementedError()

    def open_wifi_screen(self):
        """
        Open NetworkManager wifi configuration screen
        """
        raise NotImplementedError()

    def tor_connect_easy(self):
        """
        tries to connect to Tor without hiding, and with no custom configuration
        """
        raise NotImplementedError()

    def is_tor_ready(self):
        """
        checks if tor is properly connected
        """
        args = [
            "sh",
            "-c",
            ". /usr/local/lib/tails-shell-library/tor.sh; tor_is_working",
        ]
        try:
            subprocess.check_call(args)
            return True
        except subprocess.CalledProcessError:
            return False


def backoff_wait(
    total_wait: float = 30.0, initial_sleep: float = 0.5, increment=lambda x: x + 0.5
):
    total_sleep = 0
    sleep_time = initial_sleep
    while total_sleep < total_wait:
        time.sleep(sleep_time)
        total_sleep += sleep_time
        sleep_time = increment(sleep_time)
        yield


def main():
    conf, controller = recover_fd_from_parent()
    controller.authenticate(password=None)
    launcher = TorLauncherUtils(controller, conf)
    launcher.load_conf()
    print(json.dumps(launcher.tor_connection_config.to_dict(), indent=4))
    launcher.apply_conf()

    bootstrapped = False
    for _ in backoff_wait(30.0):
        is_ok = launcher.tor_has_bootstrapped()
        if is_ok:
            bootstrapped = True
            break

    if not bootstrapped:
        print("Bootstrap error!", file=sys.stderr)
        return 1

    launcher.save_conf()

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    sys.exit(main())
