# -*- coding: utf-8 -*-
"""Bitfinex API."""

#  Copyright (c) 2013 Bernd Kreuss <prof7bit@gmail.com>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

# pylint: disable=C0302,C0301,R0902,R0903,R0912,R0913,R0914,R0915,W0703,W0105

import sys
PY_VERSION = sys.version_info

if PY_VERSION < (2, 7):
    print("Sorry, minimal Python version is 2.7, you have: %d.%d"
          % (PY_VERSION.major, PY_VERSION.minor))
    sys.exit(1)

from ConfigParser import SafeConfigParser
import base64
import bisect
import binascii
import contextlib
from Crypto.Cipher import AES
import getpass
import gzip
import hashlib
import hmac
import inspect
import io
import json
import logging
import Queue
import time
import traceback
import threading
from urllib2 import Request as URLRequest
from urllib2 import urlopen, HTTPError
from urllib import urlencode
import weakref
import websocket
import math
from datetime import datetime, timedelta
import calendar

input = raw_input  # pylint: disable=W0622,C0103

FORCE_PROTOCOL = ""

#BitFinex provide depth information by orderbook:
#http://docs.bitfinex.com/#order-books
#http://docs.bitfinex.com/#orderbook
FORCE_NO_FULLDEPTH = True
FORCE_NO_DEPTH = True

#No lag api in bitfinex
#FORCE_NO_LAG = False

FORCE_NO_HISTORY = False
FORCE_HTTP_API = False
FORCE_NO_HTTP_API = False

#wss://api2.bitfinex.com:3000/ws
WEBSOCKET_HOST_PORT = "api2.bitfinex.com:3000/ws"

#https://api.bitfinex.com/v1
HTTP_API_URLS_PREFIX = "api.bitfinex.com/v1"

USER_AGENT = "bfxtool.py"


def http_request(url, post=None, headers=None):
    """request data from the HTTP API, returns the response a string. If a
    http error occurs it will *not* raise an exception, instead it will
    return the content of the error document. This is because  MtGox will
    send 5xx http status codes even if application level errors occur
    (such as canceling the same order twice or things like that) and the
    real error message will be in the json that is returned, so the return
    document is always much more interesting than the http status code."""

    def read_gzipped(response):
        """read data from the response object,
        unzip if necessary, return text string"""
        if response.info().get('Content-Encoding') == 'gzip':
            with io.BytesIO(response.read()) as buf:
                with gzip.GzipFile(fileobj=buf) as unzipped:
                    data = unzipped.read()
        else:
            data = response.read()
        return data

    if not headers:
        headers = {}
    request = URLRequest(url, post, headers)
    request.add_header('Accept-encoding', 'gzip')
    request.add_header('User-Agent', USER_AGENT)
    data = ""
    try:
        with contextlib.closing(urlopen(request, post)) as res:
            data = read_gzipped(res)
    except HTTPError as err:
        data = read_gzipped(err)

    return data

def start_thread(thread_func, name=None):
    """start a new thread to execute the supplied function"""
    thread = threading.Thread(None, thread_func)
    thread.daemon = True
    thread.start()
    if name:
        thread.name = name
    return thread

def pretty_format(something):
    """pretty-format a nested dict or list for debugging purposes.
    If it happens to be a valid json string then it will be parsed first"""
    try:
        return pretty_format(json.loads(something))
    except Exception:
        try:
            return json.dumps(something, indent=5)
        except Exception:
            return str(something)


# pylint: disable=R0904
class BfxConfig(SafeConfigParser):
    """return a config parser object with default values. If you need to run
    more Bfx() objects at the same time you will also need to give each of them
    them a separate BfxConfig() object. For this reason it takes a filename
    in its constructor for the ini file, you can have separate configurations
    for separate Bfx() instances"""

    _DEFAULTS = [["bfx", "base_currency", "BTC"]
                ,["bfx", "quote_currency", "USD"]
                ,["bfx", "use_ssl", "True"]
                ,["bfx", "use_plain_old_websocket", "True"]
                ,["bfx", "use_http_api", "True"]
                ,["bfx", "load_fulldepth", "True"]
                ,["bfx", "load_history", "True"]
                ,["bfx", "history_timeframe", "15"]
                ,["bfx", "secret_key", ""]
                ,["bfx", "secret_secret", ""]
                ]

    def __init__(self, filename):
        self.filename = filename
        SafeConfigParser.__init__(self)
        self.load()
        self.init_defaults(self._DEFAULTS)

    def init_defaults(self, defaults):
        """add the missing default values, default is a list of defaults"""
        for (sect, opt, default) in defaults:
            self._default(sect, opt, default)

    def save(self):
        """save the config to the .ini file"""
        with open(self.filename, 'wb') as configfile:
            self.write(configfile)

    def load(self):
        """(re)load the onfig from the .ini file"""
        self.read(self.filename)

    def get_safe(self, sect, opt):
        """get value without throwing exception."""
        try:
            return self.get(sect, opt)

        except: # pylint: disable=W0702
            for (dsect, dopt, default) in self._DEFAULTS:
                if dsect == sect and dopt == opt:
                    self._default(sect, opt, default)
                    return default
            return ""

    def get_bool(self, sect, opt):
        """get boolean value from config"""
        return self.get_safe(sect, opt) == "True"

    def get_string(self, sect, opt):
        """get string value from config"""
        return self.get_safe(sect, opt)

    def get_int(self, sect, opt):
        """get int value from config"""
        vstr = self.get_safe(sect, opt)
        try:
            return int(vstr)
        except ValueError:
            return 0

    def get_float(self, sect, opt):
        """get int value from config"""
        vstr = self.get_safe(sect, opt)
        try:
            return float(vstr)
        except ValueError:
            return 0.0

    def _default(self, section, option, default):
        """create a default option if it does not yet exist"""
        if not self.has_section(section):
            self.add_section(section)
        if not self.has_option(section, option):
            self.set(section, option, default)
            self.save()

class Signal():
    """callback functions (so called slots) can be connected to a signal and
    will be called when the signal is called (Signal implements __call__).
    The slots receive two arguments: the sender of the signal and a custom
    data object. Two different threads won't be allowed to send signals at the
    same time application-wide, concurrent threads will have to wait until
    the lock is releaesed again. The lock allows recursive reentry of the same
    thread to avoid deadlocks when a slot wants to send a signal itself."""

    _lock = threading.RLock()
    signal_error = None

    def __init__(self):
        self._functions = weakref.WeakSet()
        self._methods = weakref.WeakKeyDictionary()

        # the Signal class itself has a static member signal_error where it
        # will send tracebacks of exceptions that might happen. Here we
        # initialize it if it does not exist already
        if not Signal.signal_error:
            Signal.signal_error = 1
            Signal.signal_error = Signal()

    def connect(self, slot):
        """connect a slot to this signal. The parameter slot can be a funtion
        that takes exactly 2 arguments or a method that takes self plus 2 more
        arguments, or it can even be even another signal. the first argument
        is a reference to the sender of the signal and the second argument is
        the payload. The payload can be anything, it totally depends on the
        sender and type of the signal."""
        if inspect.ismethod(slot):
            instance = slot.__self__
            function = slot.__func__
            if instance not in self._methods:
                self._methods[instance] = set()
            if function not in self._methods[instance]:
                self._methods[instance].add(function)
        else:
            if slot not in self._functions:
                self._functions.add(slot)

    def __call__(self, sender, data, error_signal_on_error=True):
        """dispatch signal to all connected slots. This is a synchronuos
        operation, It will not return before all slots have been called.
        Also only exactly one thread is allowed to emit signals at any time,
        all other threads that try to emit *any* signal anywhere in the
        application at the same time will be blocked until the lock is released
        again. The lock will allow recursive reentry of the seme thread, this
        means a slot can itself emit other signals before it returns (or
        signals can be directly connected to other signals) without problems.
        If a slot raises an exception a traceback will be sent to the static
        Signal.signal_error() or to logging.critical()"""
        with self._lock:
            sent = False
            errors = []
            for func in self._functions:
                try:
                    func(sender, data)
                    sent = True

                except: # pylint: disable=W0702
                    errors.append(traceback.format_exc())

            for instance, functions in self._methods.items():
                for func in functions:
                    try:
                        func(instance, sender, data)
                        sent = True

                    except: # pylint: disable=W0702
                        errors.append(traceback.format_exc())

            for error in errors:
                if error_signal_on_error:
                    Signal.signal_error(self, (error), False)
                else:
                    logging.critical(error)

            return sent


class BaseObject():
    """This base class only exists because of the debug() method that is used
    in many of the bfxtool objects to send debug output to the signal_debug."""

    def __init__(self):
        self.signal_debug = Signal()

    def debug(self, *args):
        """send a string composed of all *args to all slots who
        are connected to signal_debug or send it to the logger if
        nobody is connected"""
        msg = " ".join([str(x) for x in args])
        if not self.signal_debug(self, (msg)):
            logging.debug(msg)


class Timer(Signal):
    """a simple timer (used for stuff like keepalive)."""

    def __init__(self, interval, one_shot=False):
        """create a new timer, interval is in seconds"""
        Signal.__init__(self)
        self._one_shot = one_shot
        self._canceled = False
        self._interval = interval
        self._timer = None
        self._start()

    def _fire(self):
        """fire the signal and restart it"""
        if not self._canceled:
            self.__call__(self, None)
            if not (self._canceled or self._one_shot):
                self._start()

    def _start(self):
        """start the timer"""
        self._timer = threading.Timer(self._interval, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        """cancel the timer"""
        self._canceled = True
        self._timer.cancel()
        self._timer = None


class Secret:
    """Manage the Bitfinex API secret. This class has methods to decrypt the
    entries in the ini file and it also provides a method to create these
    entries. The methods encrypt() and decrypt() will block and ask
    questions on the command line, they are called outside the curses
    environment (yes, its a quick and dirty hack but it works for now)."""

    S_OK            = 0
    S_FAIL          = 1
    S_NO_SECRET     = 2
    S_FAIL_FATAL    = 3

    def __init__(self, config):
        """initialize the instance"""
        self.config = config
        self.key = ""
        self.secret = ""

        # pylint: disable=C0103
        self.password_from_commandline_option = None

    #TODO ask @prof7bit to inspect this method
    def decrypt(self, password):
        """decrypt "secret_secret" from the ini file with the given password.
        This will return false if decryption did not seem to be successful.
        After this menthod succeeded the application can access the secret"""

        key = self.config.get_string("bfx", "secret_key")
        sec = self.config.get_string("bfx", "secret_secret")
        if sec == "" or key == "":
            return self.S_NO_SECRET

        # pylint: disable=E1101
        hashed_pass = hashlib.sha512(password.encode("utf-8")).digest()
        crypt_key = hashed_pass[:32]
        crypt_ini = hashed_pass[-16:]
        aes = AES.new(crypt_key, AES.MODE_OFB, crypt_ini)
        try:
            encrypted_secret = base64.b64decode(sec.strip().encode("ascii"))
            self.secret = aes.decrypt(encrypted_secret).strip()
            self.key = key.strip()
        except ValueError:
            return self.S_FAIL

        # now test if we now have something plausible
        try:
            print("testing secret...")
            # is it plain ascii? (if not this will raise exception)
            dummy = self.secret.decode("ascii")
            #TODO clarify what this code do, so bitfinex key works only when the code is commented
            # can it be decoded? correct size afterwards?
            #I don't know why but next line raise exception "Incorrect padding"
            #if len(base64.b64decode(self.secret)) != 64:
            #    raise Exception("decrypted secret has wrong size")

            print("testing key...")
            # key must be only hex digits and have the right size
            #hex_key = self.key.replace("-", "").encode("ascii")
            #if len(binascii.unhexlify(hex_key)) != 16:
            #    raise Exception("key has wrong size")

            print("ok :-)")
            return self.S_OK

        except Exception as exc:
            # this key and secret do not work :-(
            self.secret = ""
            self.key = ""
            print("### Error occurred while testing the decrypted secret:")
            print("    '%s'" % exc)
            print("    This does not seem to be a valid BitFinex API secret")
            return self.S_FAIL

    def prompt_decrypt(self):
        """ask the user for password on the command line
        and then try to decrypt the secret."""
        if self.know_secret():
            return self.S_OK

        key = self.config.get_string("bfx", "secret_key")
        sec = self.config.get_string("bfx", "secret_secret")
        if sec == "" or key == "":
            return self.S_NO_SECRET

        if self.password_from_commandline_option:
            password = self.password_from_commandline_option
        else:
            password = getpass.getpass("enter passphrase for secret: ")

        result = self.decrypt(password)
        if result != self.S_OK:
            print("")
            print("secret could not be decrypted")
            answer = input("press any key to continue anyways " \
                + "(trading disabled) or 'q' to quit: ")
            if answer == "q":
                result = self.S_FAIL_FATAL
            else:
                result = self.S_NO_SECRET
        return result

    # pylint: disable=R0201
    def prompt_encrypt(self):
        """ask for key, secret and password on the command line,
        then encrypt the secret and store it in the ini file."""
        print("Please copy/paste key and secret from BitFinex and")
        print("then provide a password to encrypt them.")
        print("")


        key =    input("             key: ").strip()
        secret = input("          secret: ").strip()
        while True:
            password1 = getpass.getpass("        password: ").strip()
            if password1 == "":
                print("aborting")
                return
            password2 = getpass.getpass("password (again): ").strip()
            if password1 != password2:
                print("you had a typo in the password. try again...")
            else:
                break

        # pylint: disable=E1101
        hashed_pass = hashlib.sha512(password1.encode("utf-8")).digest()
        crypt_key = hashed_pass[:32]
        crypt_ini = hashed_pass[-16:]
        aes = AES.new(crypt_key, AES.MODE_OFB, crypt_ini)

        # since the secret is a base64 string we can just just pad it with
        # spaces which can easily be stripped again after decryping
        print(len(secret))
        secret += " " * (16 - len(secret) % 16)
        print(len(secret))
        secret = base64.b64encode(aes.encrypt(secret)).decode("ascii")

        self.config.set("bfx", "secret_key", key)
        self.config.set("bfx", "secret_secret", secret)
        self.config.save()

        print("encrypted secret has been saved in %s" % self.config.filename)

    def know_secret(self):
        """do we know the secret key? The application must be able to work
        without secret and then just don't do any account related stuff"""
        return(self.secret != "") and (self.key != "")


class BaseClient(BaseObject):
    """abstract base class for SocketIOClient and WebsocketClient"""

    _last_unique_microtime = 0
    _nonce_lock = threading.Lock()

    def __init__(self, curr_base, curr_quote, secret, config):
        BaseObject.__init__(self)

        self.signal_recv         = Signal()
        self.signal_fulldepth    = Signal()
        self.signal_fullhistory  = Signal()
        self.signal_connected    = Signal()
        self.signal_disconnected = Signal()

        self._timer = Timer(60)
        self._timer.connect(self.slot_timer)

        self._info_timer = None # used when delayed requesting private/info

        self.curr_base = curr_base
        self.curr_quote = curr_quote

        self.currency = curr_quote # deprecated, use curr_quote instead

        self.secret = secret
        self.config = config
        self.socket = None
        self.http_requests = Queue.Queue()

        self._recv_thread = None
        self._http_thread = None
        self._terminating = False
        self.connected = False
        self._time_last_received = 0
        self._time_last_subscribed = 0
        self.history_last_candle = None

    def start(self):
        """start the client"""
        self._recv_thread = start_thread(self._recv_thread_func, "socket receive thread")
        self._http_thread = start_thread(self._http_thread_func, "http thread")

    def stop(self):
        """stop the client"""
        self._terminating = True
        self._timer.cancel()
        if self.socket:
            self.debug("### closing socket")
            self.socket.sock.close()

    def force_reconnect(self):
        """force client to reconnect"""
        self.socket.close()

    def _try_send_raw(self, raw_data):
        """send raw data to the websocket or disconnect and close"""
        if self.connected:
            try:
                self.socket.send(raw_data)
            except Exception as exc:
                self.debug(exc)
                self.connected = False
                self.socket.close()

    def send(self, json_str):
        """there exist 2 subtly different ways to send a string over a
        websocket. Each client class will override this send method"""
        raise NotImplementedError()

    def get_unique_mirotime(self):
        """produce a unique nonce that is guaranteed to be ever increasing"""
        with self._nonce_lock:
            #I don't know why but bitfinex accept nonce only from time of the future (may be timezone issue)
            since_date = datetime.utcnow() + timedelta(minutes=15*100)
            microtime  = timestamp=calendar.timegm(since_date.utctimetuple())
            #microtime = int(time.time())
            if microtime <= self._last_unique_microtime:
                microtime = self._last_unique_microtime + 1
            self._last_unique_microtime = microtime
            return microtime

    def use_http(self):
        """should we use http api? return true if yes"""
        use_http = self.config.get_bool("bfx", "use_http_api")
        if FORCE_HTTP_API:
            use_http = True
        if FORCE_NO_HTTP_API:
            use_http = False
        return use_http

    def request_fulldepth(self):
        """start the fulldepth thread"""

        def fulldepth_thread():
            """request the full market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            self.debug("### requesting initial full depth")
            use_ssl = self.config.get_bool("bfx", "use_ssl")
            proto = "https"
            fulldepth = http_request("%s://%s/book/%s%s" % (
                proto,
                HTTP_API_URLS_PREFIX,
                self.curr_base,
                self.curr_quote
            ))
            self.signal_fulldepth(self, (json.loads(fulldepth)))
            #self.debug(json.loads(fulldepth))

        start_thread(fulldepth_thread, "http request full depth")

    def request_history(self):
        """request trading history"""

        # Bfx() will have set this field to the timestamp of the last
        # known candle, so we only request data since this time
        since = self.history_last_candle

        def history_thread():
            """request trading history"""

            # 1308503626, 218868 <-- last small transacion ID
            # 1309108565, 1309108565842636 <-- first big transaction ID

            if since:
                querystring = "?timestamp=%i&limit_trades=10000" % (since * 1000000)
            else:
                #in seconds
                history_timeframe = int(self.config.get_string("bfx", "history_timeframe"))*60
                #number of chart candles is less than 100 on my display,
                # so I just multiply it with magic number
                since_date = datetime.utcnow() - timedelta(minutes=15*100)
                timestamp=calendar.timegm(since_date.utctimetuple())
                #hack with 10000 is because bitfinex api doesnt handle timestamp argument without limit_trades,
                # so I've just taken some big value to be sure that for 15min timeframes it will be enough (usually)
                querystring = "?timestamp=%i&limit_trades=10000" % timestamp
                #self.debug()

            self.debug("### requesting history")
            use_ssl = self.config.get_bool("bfx", "use_ssl")
            proto = "https"
            json_hist = http_request("%s://%s/trades/%s%s/%s" % (
                proto,
                HTTP_API_URLS_PREFIX,
                self.curr_base,
                self.curr_quote,
                querystring
            ))
            history = json.loads(json_hist)
            if history:
                #we need to reverse order of history in api
                history.reverse()
                self.signal_fullhistory(self, history)

        start_thread(history_thread, "http request trade history")

    def _recv_thread_func(self):
        """this will be executed as the main receiving thread, each type of
        client (websocket or socketio) will implement its own"""
        raise NotImplementedError()

    def channel_subscribe(self, download_market_data=True):
        """subscribe to needed channnels and download initial data (orders,
        account info, depth, history, etc. Some of these might be redundant but
        at the time I wrote this code the socketio server seemed to have a bug,
        not being able to subscribe via the GET parameters, so I send all
        needed subscription requests here again, just to be on the safe side."""

        symb = "%s%s" % (self.curr_base, self.curr_quote)

        self.send(json.dumps({
            "event": "subscribe",
            "channel": "ticker",
            "pair": symb
        }))

        # trades and lag are the same channels for all currencies
        self.send(json.dumps({
            "event": "subscribe",
            "channel": "trades",
            "pair": symb
        }))

        #see http://docs.bitfinex.com/#order-books
        self.send(json.dumps({
            "event": "subscribe",
            "channel": "book",
            "pair": symb,
            "prec": "P0"
        }))

        self.request_orders()
        self.request_info()

        if download_market_data:
            if self.config.get_bool("bfx", "load_fulldepth"):
                if not FORCE_NO_FULLDEPTH:
                    self.request_fulldepth()
            if self.config.get_bool("bfx", "load_history"):
                if not FORCE_NO_HISTORY:
                    self.request_history()


        self.subscribe_auth_account_info_channel()

        self._time_last_subscribed = time.time()

    def subscribe_auth_account_info_channel(self):
        """
        method for subscribing for account info channel
        http://docs.bitfinex.com/#account-info

        send a signed (authenticated) API call over the Websocket.
        This method will only succeed if the secret key is available,
        otherwise it will just log a warning and do nothing."""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot subscribe account info channel ")
            return
        key = self.secret.key
        sec = self.secret.secret
        payload = 'AUTH%d' % self.get_unique_mirotime()
        signature = hmac.new(sec, payload, hashlib.sha384).hexdigest()
        #self.debug("### (socket) calling %s" % api_endpoint)
        self.send(json.dumps({
            'event': "auth",
            'apiKey': key,
            'authSig': signature,
            'authPayload': payload,
        }))


    def _slot_timer_info_later(self, _sender, _data):
        """the slot for the request_info_later() timer signal"""
        self.request_info()
        self._info_timer = None

    def request_info_later(self, delay):
        """request the private/info in delay seconds from now"""
        if self._info_timer:
            self._info_timer.cancel()
        self._info_timer = Timer(delay, True)
        self._info_timer.connect(self._slot_timer_info_later)

    def request_info(self):
        """request
                the account_infos object (for getting fees) and
                the balances object
        """
        #do we need here http://docs.bitfinex.com/#wallet-balances ????

        self.enqueue_http_request("account_infos", {}, "account_infos")
        self.enqueue_http_request("balances", {}, "balances")
        self.debug("requesting info via http")

    def request_orders(self):
        """request the private/orders object"""
        #if self.use_http():
        self.enqueue_http_request("orders", {}, "orders")
        #else:
        #    self.send_signed_call("orders", {}, "orders")

    def _http_thread_func(self):
        """send queued http requests to the http API (only used when
        http api is forced, normally this is much slower)"""
        while not self._terminating:
            # pop queued request from the queue and process it
            (api_endpoint, params, reqid) = self.http_requests.get(True)
            translated = None
            try:
                answer = self.http_signed_call(api_endpoint, params)

                self.debug(answer)
                self.debug(api_endpoint)
                self.debug(params)
                self.debug(reqid)

                translated = {
                    "reqid": reqid,
                    "data": answer,
                }

                #if answer["result"] == "success":
                #    # the following will reformat the answer in such a way
                #    # that we can pass it directly to signal_recv()
                #    # as if it had come directly from the websocket
                #    translated = {
                #        "op": "result",
                #        "result": answer["data"],
                #        "id": reqid
                #    }
                #else:
                #    if "error" in answer:
                #        if answer["token"] == "unknown_error":
                #            # enqueue it again, it will eventually succeed.
                #            self.enqueue_http_request(api_endpoint, params, reqid)
                #        else:
                #
                #            # these are errors like "Order amount is too low"
                #            # or "Order not found" and the like, we send them
                #            # to signal_recv() as if they had come from the
                #            # streaming API beause Bfx() can handle these errors.
                #            translated = {
                #                "op": "remark",
                #                "success": False,
                #                "message": answer["error"],
                #                "token": answer["token"],
                #                "id": reqid
                #            }
                #
                #    else:
                #        self.debug("### unexpected http result:", answer, reqid)

            except Exception as exc:
                # should this ever happen? HTTP 5xx wont trigger this,
                # something else must have gone wrong, a totally malformed
                # reply or something else.
                #
                # After some time of testing during times of heavy
                # volatility it appears that this happens mostly when
                # there is heavy load on their servers. Resubmitting
                # the API call will then eventally succeed.
                self.debug("### exception in _http_thread_func:",
                    exc, api_endpoint, params, reqid)

                # enqueue it again, it will eventually succeed.
                self.enqueue_http_request(api_endpoint, params, reqid)

            if translated:
                self.signal_recv(self, (json.dumps(translated)))

            self.http_requests.task_done()

    def enqueue_http_request(self, api_endpoint, params, reqid):
        """enqueue a request for sending to the HTTP API, returns
        immediately, behaves exactly like sending it over the websocket."""
        if self.secret and self.secret.know_secret():
            self.http_requests.put((api_endpoint, params, reqid))

    def http_signed_call(self, api_endpoint, params):
        """send a signed request to the HTTP API"""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        params["nonce"] = "%d" % self.get_unique_mirotime()
        params["request"] = '/v1/%s' % api_endpoint

        payload = base64.b64encode(json.dumps(params, ensure_ascii=False))
        signature = hmac.new(sec, payload, hashlib.sha384).hexdigest()
        post = payload
        headers = {
            'X-BFX-APIKEY': key,
            'X-BFX-PAYLOAD': payload,
            'X-BFX-SIGNATURE': signature,
        }

        #use_ssl = self.config.get_bool("bfx", "use_ssl")
        proto = "https"
        #proto = {True: "https", False: "http"}[use_ssl]
        url = "%s://%s/%s" % (
            proto,
            HTTP_API_URLS_PREFIX,
            api_endpoint
        )
        self.debug("### (%s) calling %s" % (proto, url))
        return json.loads(http_request(url, post, headers))

    #def send_signed_call(self, api_endpoint, params, reqid):
    #    """send a signed (authenticated) API call over the socket.io.
    #    This method will only succeed if the secret key is available,
    #    otherwise it will just log a warning and do nothing."""
    #    if (not self.secret) or (not self.secret.know_secret()):
    #        self.debug("### don't know secret, cannot call %s" % api_endpoint)
    #        return
    #
    #    key = self.secret.key
    #    sec = self.secret.secret
    #
    #    call = {
    #        "id"       : reqid,
    #        "call"     : api_endpoint,
    #        "params"   : params,
    #        "currency" : self.curr_quote,
    #        "item"     : self.curr_base
    #    }
    #    call["nonce"] = self.get_unique_mirotime()
    #    call = json.dumps(call)
    #
    #    # pylint: disable=E1101
    #    sign = hmac.new(base64.b64decode(sec), call, hashlib.sha512).digest()
    #    signedcall = key.replace("-", "").decode("hex") + sign + call
    #
    #    self.debug("### (socket) calling %s" % api_endpoint)
    #    self.send(json.dumps({
    #        "op"      : "call",
    #        "call"    : base64.b64encode(signedcall),
    #        "id"      : reqid,
    #        "context" : "mtgox.com"
    #    }))

    def send_order_add(self, typ, price, volume):
        """send an order"""
        #raise Exception("Not implemented send_order_add")
        reqid = "order_add:%s:%f:%f" % (typ, price, volume)

        symb = "%s%s" % (self.curr_base, self.curr_quote)
        buy_or_sell = {'bid': 'buy', 'ask': 'sell'}[typ]
        params = {
            "request": "/v1/order/new",
            "nonce": self.get_unique_mirotime(),
            "symbol": symb,
            "amount": "%f" % volume,
            "price": "%f" % price,
            "exchange": "bitfinex",
            "side": buy_or_sell,
            "type": "exchange limit"
        }
        api = "order/new"
        self.enqueue_http_request(api, params, reqid)

    def send_order_cancel(self, oid):
        """cancel an order"""
        params = {
            "request": "/v1/order/cancel",
            "nonce": self.get_unique_mirotime(),
            "order_id": oid}
        reqid = "order_cancel:%s" % oid
        #if self.use_http():
        api = "order/cancel"
        self.enqueue_http_request(api, params, reqid)
        #else:
        #    api = "order/cancel"
        #    self.send_signed_call(api, params, reqid)

    def slot_timer(self, _sender, _data):
        """check timeout (last received, dead socket?)"""
        if self.connected:
            if time.time() - self._time_last_received > 60:
                self.debug("### did not receive anything for a long time, disconnecting.")
                self.force_reconnect()
                self.connected = False
            if time.time() - self._time_last_subscribed > 1800:
                # sometimes after running for a few hours it
                # will lose some of the subscriptons for no
                # obvious reason. I've seen it losing the trades
                # and the lag channel channel already, and maybe
                # even others. Simply subscribing again completely
                # fixes this condition. For this reason we renew
                # all channel subscriptions once every hour.
                self.debug("### refreshing channel subscriptions")
                self.channel_subscribe(False)


class WebsocketClient(BaseClient):
    """this implements a connection to BitFinex through the websocket protocol."""
    def __init__(self, curr_base, curr_quote, secret, config):
        BaseClient.__init__(self, curr_base, curr_quote, secret, config)
        self.hostname = WEBSOCKET_HOST_PORT

    def _recv_thread_func(self):
        """connect to the websocket and start receiving in an infinite loop.
        Try to reconnect whenever connection is lost. Each received json
        string will be dispatched with a signal_recv signal"""
        reconnect_time = 1
        use_ssl = self.config.get_bool("bfx", "use_ssl")
        ##wsp = {True: "wss://", False: "ws://"}[use_ssl]
        wsp = "wss://"
        #port = {True: 443, False: 80}[use_ssl]
        #port = 3000
        #ws_origin = "%s:%d" % (self.hostname, port)
        ws_origin = self.hostname
        ws_headers = ["User-Agent: %s" % USER_AGENT]
        while not self._terminating:  #loop 0 (connect, reconnect)
            try:
                # channels separated by "/", wildcards allowed. Available
                # channels see here: https://mtgox.com/api/2/stream/list_public
                # example: ws://websocket.mtgox.com/?Channel=depth.LTCEUR/ticker.LTCEUR
                # the trades and lag channel will be subscribed after connect
                sym = "%s%s" % (self.curr_base, self.curr_quote)
                ws_url = "%s%s" % (wsp, self.hostname)
                self.debug("### trying plain old Websocket: %s ... " % ws_url)

                self.socket = websocket.WebSocket()
                # The server is somewhat picky when it comes to the exact
                # host:port syntax of the origin header, so I am supplying
                # my own origin header instead of the auto-generated one
                self.socket.connect(ws_url, origin=ws_origin, header=ws_headers)
                self._time_last_received = time.time()
                self.connected = True
                self.debug("### connected, subscribing needed channels")
                self.channel_subscribe()
                self.debug("### waiting for data...")
                self.signal_connected(self, None)
                while not self._terminating: #loop1 (read messages)
                    str_json = self.socket.recv()
                    self._time_last_received = time.time()
                    #if str_json[0] == "{":
                    self.signal_recv(self, (str_json))
                    #else:
                    #    self.debug(str_json)

            except Exception as exc:
                self.connected = False
                self.signal_disconnected(self, None)
                if not self._terminating:
                    self.debug("### ", exc.__class__.__name__, exc,
                        "reconnecting in %i seconds..." % reconnect_time)
                    if self.socket:
                        self.socket.close()
                    time.sleep(reconnect_time)

    def send(self, json_str):
        """send the json encoded string over the websocket"""
        self._try_send_raw(json_str)


class OHLCV():
    """represents a chart candle. tim is POSIX timestamp of open time,
    prices and volume are integers like in the other parts of the bfx API"""

    def __init__(self, tim, opn, hig, low, cls, vol):
        self.tim = tim
        self.opn = opn
        self.hig = hig
        self.low = low
        self.cls = cls
        self.vol = vol

    def update(self, price, volume):
        """update high, low and close values and add to volume"""
        if price > self.hig:
            self.hig = price
        if price < self.low:
            self.low = price
        self.cls = price
        self.vol += volume


class History(BaseObject):
    """represents the trading history"""

    def __init__(self, bfx, timeframe):
        BaseObject.__init__(self)

        self.signal_fullhistory_processed = Signal()
        self.signal_changed               = Signal()

        self.bfx = bfx
        self.candles = []
        self.timeframe = timeframe

        self.ready_history = False

        bfx.signal_trade.connect(self.slot_trade)
        bfx.signal_fullhistory.connect(self.slot_fullhistory)

    def add_candle(self, candle):
        """add a new candle to the history"""
        self._add_candle(candle)
        self.signal_changed(self, (self.length()))

    def slot_trade(self, dummy_sender, data):
        """slot for bfx.signal_trade"""
        (date, price, volume, dummy_typ, own) = data
        if not own:
            time_round = int(date / self.timeframe) * self.timeframe
            candle = self.last_candle()
            if candle:
                if candle.tim == time_round:
                    candle.update(price, volume)
                    self.signal_changed(self, (1))
                else:
                    self.debug("### opening new candle")
                    self.add_candle(OHLCV(
                        time_round, price, price, price, price, volume))
            else:
                self.add_candle(OHLCV(
                    time_round, price, price, price, price, volume))

    def _add_candle(self, candle):
        """add a new candle to the history but don't fire signal_changed"""
        self.candles.insert(0, candle)

    def slot_fullhistory(self, dummy_sender, data):
        """process the result of the fullhistory request"""
        (history) = data

        if not len(history):
            self.debug("### history download was empty")
            return

        def get_time_round(date):
            """round timestamp to current candle timeframe"""
            return int(date / self.timeframe) * self.timeframe

        #remove existing recent candle(s) if any, we will create them fresh
        date_begin = get_time_round(int(history[0]["timestamp"]))
        while len(self.candles) and self.candles[0].tim >= date_begin:
            self.candles.pop(0)

        new_candle = OHLCV(0, 0, 0, 0, 0, 0) #this is a dummy, not actually inserted
        count_added = 0
        for trade in history:
            date = int(trade["timestamp"])
            price = float(trade["price"])
            volume = float(trade["amount"])
            time_round = get_time_round(date)
            #self.debug("timeround is %d" % time_round)
            #self.debug("new_candle.tim is %d" % new_candle.tim)
            if time_round > new_candle.tim:
                if new_candle.tim > 0:
                    self._add_candle(new_candle)
                    count_added += 1
                new_candle = OHLCV(
                    time_round, price, price, price, price, volume)
            new_candle.update(price, volume)
        #self.debug("len of history is %d" % len(history))
        # insert current (incomplete) candle
        self._add_candle(new_candle)
        count_added += 1
        self.debug("### got %d updated candle(s)" % count_added)
        self.ready_history = True
        self.signal_fullhistory_processed(self, None)
        self.signal_changed(self, (self.length()))

    def last_candle(self):
        """return the last (current) candle or None if empty"""
        if self.length() > 0:
            return self.candles[0]
        else:
            return None

    def length(self):
        """return the number of candles in the history"""
        return len(self.candles)


# pylint: disable=R0902
class Bfx(BaseObject):
    """represents the API of the Bitfexchange. An Instance of this
    class will connect to the streaming socket.io API, receive live
    events, it will emit signals you can hook into for all events,
    it has methods to buy and sell"""

    def __init__(self, secret, config):
        """initialize the bfx API but do not yet connect to it."""
        BaseObject.__init__(self)

        self.signal_depth           = Signal()
        self.signal_trade           = Signal()
        self.signal_ticker          = Signal()
        self.signal_fulldepth       = Signal()
        self.signal_fullhistory     = Signal()
        self.signal_wallet          = Signal()
        self.signal_userorder       = Signal()
        self.signal_orderlag        = Signal()
        self.signal_disconnected    = Signal() # socket connection lost
        self.signal_ready           = Signal() # connected and fully initialized

        self.signal_order_too_fast  = Signal() # don't use that

        self.strategies = weakref.WeakValueDictionary()

        # the following are not fired by bfx itself but by the
        # application controlling it to pass some of its events
        self.signal_keypress        = Signal()
        self.signal_strategy_unload = Signal()

        self._idkey      = ""
        self.wallet = {}
        self.trade_fee = 0  # percent (float, for example 0.6 means 0.6%)
        self.monthly_volume = 0 # BTC (satoshi int)
        self.order_lag = 0  # microseconds
        self.socket_lag = 0 # microseconds
        self.last_tid = 0
        self.count_submitted = 0  # number of submitted orders not yet acked
        self.msg = {} # the incoming message that is currently processed

        # the following will be set to true once the information
        # has been received after connect, once all thes flags are
        # true it will emit the signal_connected.
        self.ready_idkey = False
        self.ready_info = False
        self._was_disconnected = True

        self.config = config
        self.curr_base = config.get_string("bfx", "base_currency")
        self.curr_quote = config.get_string("bfx", "quote_currency")

        self.currency = self.curr_quote # deprecated, use curr_quote instead

        # these are needed for conversion from/to intereger, float, string
        self.mult_quote = 1e0
        self.format_quote = "%12.5f"
        self.mult_base = 1e0
        self.format_base = "%16.8f"

        Signal.signal_error.connect(self.signal_debug)

        timeframe = 60 * config.get_int("bfx", "history_timeframe")
        if not timeframe:
            timeframe = 60 * 15
        self.history = History(self, timeframe)
        self.history.signal_debug.connect(self.signal_debug)

        self.orderbook = OrderBook(self)
        self.orderbook.signal_debug.connect(self.signal_debug)

        use_websocket = self.config.get_bool("bfx", "use_plain_old_websocket")
        use_websocket = True
        self.client = WebsocketClient(self.curr_base, self.curr_quote, secret, config)
        #pairs of chanId - channel names
        #http://docs.bitfinex.com/#authenticated-channels73
        #public channels are dynamic
        self.channels = {0: "auth",}

        self.client.signal_debug.connect(self.signal_debug)
        self.client.signal_disconnected.connect(self.slot_disconnected)
        self.client.signal_connected.connect(self.slot_client_connected)
        self.client.signal_recv.connect(self.slot_recv)
        self.client.signal_fulldepth.connect(self.signal_fulldepth)
        self.client.signal_fullhistory.connect(self.signal_fullhistory)

        self.timer_poll = Timer(120)
        self.timer_poll.connect(self.slot_poll)

        self.history.signal_changed.connect(self.slot_history_changed)
        self.history.signal_fullhistory_processed.connect(self.slot_fullhistory_processed)
        self.orderbook.signal_fulldepth_processed.connect(self.slot_fulldepth_processed)
        self.orderbook.signal_owns_initialized.connect(self.slot_owns_initialized)

    def start(self):
        """connect to BitFinex and start receiving events."""
        self.debug("### starting bfx streaming API, trading %s%s" %
            (self.curr_base, self.curr_quote))
        self.client.start()

    def stop(self):
        """shutdown the client"""
        self.debug("### shutdown...")
        self.client.stop()

    def order(self, typ, price, volume):
        """place pending order. If price=0 then it will be filled at market"""
        self.count_submitted += 1
        self.client.send_order_add(typ, price, volume)

    def buy(self, price, volume):
        """new buy order, if price=0 then buy at market"""
        self.order("bid", price, volume)

    def sell(self, price, volume):
        """new sell order, if price=0 then sell at market"""
        self.order("ask", price, volume)

    def cancel(self, oid):
        """cancel order"""
        self.client.send_order_cancel(oid)

    def cancel_by_price(self, price):
        """cancel all orders at price"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if order.price == price:
                if order.oid != "":
                    self.cancel(order.oid)

    def cancel_by_type(self, typ=None):
        """cancel all orders of type (or all orders if typ=None)"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if typ == None or typ == order.typ:
                if order.oid != "":
                    self.cancel(order.oid)

    def base2float(self, int_number):
        """convert base currency values from mtgox integer to float. Base
        currency are the coins you are trading (BTC, LTC, etc). Use this method
        to convert order volumes (amount of coins) from int to float."""
        return float(int_number) / self.mult_base

    def base2str(self, int_number):
        """convert base currency values from mtgox integer to formatted string"""
        return self.format_base % (float(int_number) / self.mult_base)

    def base2int(self, float_number):
        """convert base currency values from float to mtgox integer"""
        return int(round(float_number * self.mult_base))

    def quote2float(self, int_number):
        """convert quote currency values from mtgox integer to float. Quote
        currency is the currency used to quote prices (USD, EUR, etc), use this
        method to convert the prices of orders, bid or ask from int to float."""
        return float(int_number) / self.mult_quote

    def quote2str(self, int_number):
        """convert quote currency values from mtgox integer to formatted string"""
        return self.format_quote % (float(int_number) / self.mult_quote)

    def quote2int(self, float_number):
        """convert quote currency values from float to mtgox integer"""
        return int(round(float_number * self.mult_quote))

    def check_connect_ready(self):
        """check if everything that is needed has been downloaded
        and emit the connect signal if everything is ready"""
        need_no_account = not self.client.secret.know_secret()
        need_no_depth = not self.config.get_bool("bfx", "load_fulldepth")
        need_no_history = not self.config.get_bool("bfx", "load_history")
        need_no_depth = need_no_depth or FORCE_NO_FULLDEPTH
        need_no_history = need_no_history or FORCE_NO_HISTORY
        ready_account = \
            self.ready_idkey and self.ready_info and self.orderbook.ready_owns
        if ready_account or need_no_account:
            if self.orderbook.ready_depth or need_no_depth:
                if self.history.ready_history or need_no_history:
                    if self._was_disconnected:
                        self.signal_ready(self, None)
                        self._was_disconnected = False

    def slot_client_connected(self, _sender, _data):
        """connected to the client"""
        self.check_connect_ready()

    def slot_fulldepth_processed(self, _sender, _data):
        """connected to the orderbook"""
        self.check_connect_ready()

    def slot_fullhistory_processed(self, _sender, _data):
        """connected to the history"""
        self.check_connect_ready()

    def slot_owns_initialized(self, _sender, _data):
        """connected to the orderbook"""
        self.check_connect_ready()

    def slot_disconnected(self, _sender, _data):
        """this slot is connected to the client object, all it currently
        does is to emit a disconnected signal itself"""
        self.ready_idkey = False
        self.ready_info = False
        self.orderbook.ready_owns = False
        self.orderbook.ready_depth = False
        self.history.ready_history = False
        self._was_disconnected = True
        self.signal_disconnected(self, None)

    def slot_recv(self, dummy_sender, data):
        """Slot for signal_recv, handle new incoming JSON message. Decode the
        JSON string into a Python object and dispatch it to the method that
        can handle it."""
        (str_json) = data
        handler = None
        if type(str_json) == dict:
            msg = str_json # was already a dict
        else:
            msg = json.loads(str_json)
        self.msg = msg

        if "event" in msg:
            #public channels send first messages with chanId and event
            try:
                msg_op = msg["event"]
                handler = getattr(self, "_on_event_" + msg_op)

            except AttributeError:
                #self.debug("slot_recv() ignoring: op=%s" % msg_op)
                self.debug("slot_recv() ignoring: opachki=%s" % msg)
        elif isinstance(msg, list) and msg[1] == 'hb':
            #Heartbeat messages to be ignored
            # http://docs.bitfinex.com/#heartbeating
            #[7, u'hb']
            #[6, u'hb']
            #self.debug("Heartbeat from channel: %d (%s)" % (msg[0],self.channels[msg[0]]))
            pass
        elif str_json[0] == '[':
            #parse channel
            #self.debug(msg)
            chan_name = self.channels[msg[0]]
            handler = getattr(self, "_on_channel_" + chan_name)
            #possible messages are trades and ticker
            #and depth (order book on bitfinex)
        elif "reqid" in msg:
            #then we handle it as orders, account_info, balances messages
            if msg['reqid'] == "orders":
                self._on_http_reqid_orders(msg)
            elif "order_add" in msg['reqid']:
                self._on_http_reqid_order_add(msg)
            elif "order_cancel" in msg['reqid']:
                self._on_http_reqid_order_cancel(msg)
            elif msg['reqid'] == "account_infos":
                self._on_http_reqid_account_infos(msg)
            elif msg['reqid'] == "balances":
                self._on_http_reqid_balances(msg)
            else:
                self.debug("slot_recv() ignoring: REQID", msg)
        else:
            self.debug("slot_recv() ignoring:", msg)

        if handler:
            handler(msg)

    def slot_poll(self, _sender, _data):
        """poll stuff from http in regular intervals, not yet implemented"""
        if self.client.secret and self.client.secret.know_secret():
            # poll recent own trades
            # fixme: how do i do this, whats the api for this?
            pass

    def slot_history_changed(self, _sender, _data):
        """this is a small optimzation, if we tell the client the time
        of the last known candle then it won't fetch full history next time"""
        last_candle = self.history.last_candle()
        if last_candle:
            self.client.history_last_candle = last_candle.tim

    def _on_event_subscribed(self, msg):
        """handle subscribed messages (event:subscribed)"""
        self.debug("### subscribed channel", msg["channel"], msg["chanId"])
        self.channels[msg['chanId']] = msg['channel']

    def _on_event_info(self, msg):
        """handle info message of bitfinex"""
        self.debug("### info", msg)

    def _on_channel_trades(self, msg):
        """handle message about trades

        trades:
            http://docs.bitfinex.com/#trades71
        example for updates:
            [6, u'312653-BTCUSD', 1448398210, 319.97, 0.40357]
            [<chanId>, <Seq>, <UnixTimestamp>, <Price>, <Amount>]
        example for first reply:
            [6,
            [[u'312849-BTCUSD', 1448402940, 321.09, 0.01183],
            [u'312848-BTCUSD', 1448402940, 321.04, 0.0619],
            [u'312847-BTCUSD', 1448402784, 320.81, 0.0769],
            [u'312846-BTCUSD', 1448402784, 320.69, 0.36],
            [u'312845-BTCUSD', 1448402784, 320.67, 1.533],
            [u'312844-BTCUSD', 1448402784, 320.67, 3.0301],
            ...
            [u'312825-BTCUSD', 1448401916, 321, -3]]]
        Amount is negative when sell (ask) trades and postitive when buys (bid) happen.
        """
        if len(msg) == 2:
            #first message
            #But we processed these trades in first http request, so we may ignore it
            #self.debug(msg)
            #self.debug("Len of websocket history is %d" % len(msg[1]))
            return
        #listen to updates of trades only

        #self.debug(msg)

        date = int(msg[2])
        price = float(msg[3])
        volume = math.fabs(float(msg[4]))
        if float(msg[4]) > 0:
            typ = 'bid'
        else:
            typ = 'ask'

        #TODO handle own trades?
        own = False
        if own:
            self.debug("trade: %s: %s @ %s (own order filled)" % (
                typ,
                self.base2str(volume),
                self.quote2str(price)
            ))
            # send another private/info request because the fee might have
            # changed. We request it a minute later because the server
            # seems to need some time until the new values are available.
            self.client.request_info_later(60)
        else:
            self.debug("trade: %s: %s @ %s" % (
                typ,
                self.base2str(volume),
                self.quote2str(price)
            ))

        self.signal_trade(self, (date, price, volume, typ, own))

    def _on_channel_ticker(self, msg):
        """handle incoming ticker message

        #ticker:
        # http://docs.bitfinex.com/#ticker72
        # [7, 319.96, 25, 319.97, 0.71814844, -4.57, -0.01, 319.97, 9250.85993235, 325.13, 315.55]
        #[<CHANNEL_ID>, <BID>, <BID_SIZE>, <ASK>, <ASK_SIZE>, <DAILY_CHANGE>, <DAILY_CHANGE_PERC>,
        # <LAST_PRICE>, <VOLUME>, <HIGH>, <LOW>]
        """
        #self.debug("Ticker message: %s" % msg)
        bid = int(msg[1])
        ask = int(msg[3])

        self.debug(" tick: %s %s" % (
            self.quote2str(bid),
            self.quote2str(ask)
        ))
        self.signal_ticker(self, (bid, ask))

    def _on_channel_book(self, msg):
        """handle incoming depth (book) message

        depth messages are of two types:
         - bulky (first message, with list [<CHANNEL_ID>, <list of orderbooks>]) and
         - updates ["<CHANNEL_ID>","<PRICE>","<COUNT>","<AMOUNT>"]
        we ignore bulky message (because we request fulldepth by http like we done it with trades)
        and handle only update messages
        """
        if len(msg) == 2:
            return
        if float(msg[3]) > 0:
            typ = 'bid'
        else:
            typ = 'ask'
        price = float(msg[1])
        volume = math.fabs(float(msg[3]))
        total_volume = math.fabs(float(msg[3]))

        self.signal_depth(self, (typ, price, volume, total_volume))

    def _on_channel_auth(self, msg):
        """handle incoming messages from account info chanel (authenticated channel)
        docs: http://docs.bitfinex.com/#authenticated-channels73
        """
        self.debug("_on_channel_auth")
        # there is a plenty of messages on the channel:
        # ws	wallet snapshot
        # wu	wallet update
        # os	order snapshot
        # on	new order
        # ou	order update
        # oc	order cancel
        # te	trade executed
        # tu	trade execution update
        # ts    trade snapshot
        #Those we ignore:
        # ps    position snapshot
        # pn    new position
        # pu	position update
        # pc	position close
        #Also I've seen:
        # hos   ???
        type_code = msg[1]
        #handler = None
        #try:
        #    handler = getattr(self, "_on_channel_auth_" + auth_message_type_code)
        #except AttributeError:
        #    self.debug("### ignored (%s) authenticated_account_info message" % msg[1].upper())
        ##    self.debug(pretty_format(msg[2]))
        #
        self.debug(pretty_format(msg))
        if type_code  == 'on':
            self.debug("### catched a ORDER NEW in websocket")
        elif type_code  == 'ou':
            self.debug("### catched an ORDER UPDATE in websocket")
        elif type_code == 'oc':
            self.debug("### catched an ORDER CANCEL in websocket")
            self._on_channel_auth_oc(msg)
        elif type_code == 'os':
            self.debug("### catched an ORDER SNAPSHOT in websocket")

        elif type_code == 'ws':
            self.debug("### catched the WALLET SNAPSHOT in websocket")
        elif type_code == 'wu':
            self.debug("### catched the WALLET UPDATE in websocket")
            self._on_channel_auth_wu(msg)

        elif type_code == 'tu':
            self.debug("### catched the TRADE EXECUTION UPDATE in websocket")
        elif type_code == 'te':
            self.debug("### catched the TRADE EXECUTED in websocket")
        #else:
        #    #ignore
        #    self.debug("### ignored (%s) authenticated_account_info message" % msg[1])


    def _on_channel_auth_wu(self, msg):
        """
        handle incoming wallet update message
        [
           0,
           "wu",
           [
              "<WLT_NAME>",
              "<WLT_CURRENCY>",
              "<WLT_BALANCE>",
              "<WLT_INTEREST_UNSETTLED>"
           ]
        ]
        Snapshot:
        [
           0,
           "ws",
           [
               [
                  "<WLT_NAME>",
                  "<WLT_CURRENCY>",
                  "<WLT_BALANCE>",
                  "<WLT_INTEREST_UNSETTLED>"
               ],
               [
               ...
               ]
        ]

        """
        updated_wallet = msg[2]
        updated_currency = updated_wallet[1]
        updated_balance = updated_wallet[2]
        self.wallet[updated_currency.upper()] = updated_balance
        self.signal_wallet(self, None)


    def _on_channel_auth_on(self, msg):

        self.debug("### ON Event")

    def _on_channel_auth_ou(self, msg):

        self.debug("### OU Event")

    def _on_channel_auth_oc(self, msg):
        """
        order cancelled (when filled)
        statuses = ACTIVE, EXECUTED, PARTIALLY FILLED, ...
        see: http://docs.bitfinex.com/#account-info
        [
           0,
           "<on|ou|oc>",
           [
              "<ORD_ID>",
              "<ORD_PAIR>",
              "<ORD_AMOUNT>",
              "<ORD_AMOUNT_ORIG>",
              "<ORD_TYPE>",
              "<ORD_STATUS>",
              "<ORD_PRICE>",
              "<ORD_PRICE_AVG>",
              "<ORD_CREATED_AT>",
              "<ORD_NOTIFY>",
              "<ORD_HIDDEN>"
           ]
        ]
        """
        self.debug("### OC Event")
        order = msg[2]
        oid = order[0]
        status = order[5]
        if "EXECUTED" in status and self.orderbook.have_own_oid(oid):
            # these are remove messages (cancel or fill)
            # here it is a bit more expensive to check whether they belong to
            # this bfx instance, they don't carry any other useful data besides
            # the order id and the remove reason but since a remove message can
            # only affect us if the oid is in the owns list already we just
            # ask the orderbook instance whether it knows about this order
            # and ignore all the ones that have unknown oid

            # they don't contain a status field either, so we make up
            # our own status string to make it more useful. It will
            # be "removed:" followed by the reason. Possible reasons are:
            # "requested", "completed_passive", "completed_active"
            # so for example a cancel would be "removed:requested"
            # and a limit order fill would be "removed:completed_passive".
            status = "removed:completed_active"
            self.signal_userorder(self, (0, 0, "", oid, status))
            self.debug("### Order %d is %s!!!!!" % (oid, status))
        #self.debug(msg)

    def _on_http_reqid_orders(self, msg):
        self.debug("ORDERS")
        self.debug("### got own order list")
        self.debug(msg)

    def _on_http_reqid_balances(self, msg):
        #self.debug("BALANCES")
        self.debug("### got balances")
        self.debug(msg)
        bfx_wallet = msg["data"]
        self.wallet = {}
        for currency in bfx_wallet:
            self.wallet[currency['currency'].upper()] = float(
                currency["amount"])

        self.signal_wallet(self, None)
        self.ready_info = True
        self.check_connect_ready()


    def _on_http_reqid_account_infos(self, msg):
        self.debug("### got account infos")
        self.debug(msg)
        self.trade_fee = float(msg["data"][0]['taker_fees'])

    def _on_http_reqid_order_add(self, msg):

        order = msg["data"]
        # these are limit orders.
        #
        # we also need to check whether they belong to our own bfx instance,
        # since they contain currency this is easy, we compare the currency
        # and simply ignore mesages for all unrelated currencies.
        symb = "%s%s" % (self.curr_base, self.curr_quote)
        #self.debug("ORDER NEW2", order["symbol"], symb.lower())
        if order["symbol"] == symb.lower():

            oid = order["id"]
            price = float(order["price"])
            volume = float(order["original_amount"])
            typ = {"buy": "bid", "sell": "ask"}[order["side"]]
            status = "open"
            self.signal_userorder(self, (price, volume, typ, oid, status))
            # order/add has been acked and we got an oid, now we can already
            # insert a pending order into the owns list (it will be pending
            # for a while when the server is busy but the most important thing
            # is that we have the order-id already).
            #parts = reqid.split(":")
            #typ = parts[1]
            #price = float(parts[2])
            #volume = float(parts[3])
            #oid = result
            self.debug(msg)
            self.debug("### got ack for order/add:", typ, price, volume, oid)
            self.count_submitted -= 1

            self.orderbook.add_own(Order(price, volume, typ, oid, status))

        self.debug(msg)

    def _on_http_reqid_order_cancel(self, msg):
        #self.debug("ORDER_CANCEL")
        order = msg["data"]
        oid = order["id"]
        # these are remove messages (cancel or fill)
        # here it is a bit more expensive to check whether they belong to
        # this bfx instance, they don't carry any other useful data besides
        # the order id and the remove reason but since a remove message can
        # only affect us if the oid is in the owns list already we just
        # ask the orderbook instance whether it knows about this order
        # and ignore all the ones that have unknown oid
        if self.orderbook.have_own_oid(oid):
            # they don't contain a status field either, so we make up
            # our own status string to make it more useful. It will
            # be "removed:" followed by the reason. Possible reasons are:
            # "requested", "completed_passive", "completed_active"
            # so for example a cancel would be "removed:requested"
            # and a limit order fill would be "removed:completed_passive".
            status = "removed:requested"
            self.signal_userorder(self, (0, 0, "", oid, status))

        self.debug(msg)

    def _on_op_error(self, msg):
        """handle error mesages (op:error)"""
        self.debug("### _on_op_error()", msg)

    def _on_op_result(self, msg):
        """handle result of authenticated API call (op:result, id:xxxxxx)"""
        result = msg["result"]
        reqid = msg["id"]

        if reqid == "orders":
            self.debug("### got own order list")
            self.count_submitted = 0
            self.orderbook.init_own(result)
            self.debug("### have %d own orders for %s/%s" %
                (len(self.orderbook.owns), self.curr_base, self.curr_quote))

        elif reqid == "info":
            self.debug("### got account info")
            bfx_wallet = result["Wallets"]
            self.wallet = {}
            self.monthly_volume = int(result["Monthly_Volume"]["value_int"])
            self.trade_fee = float(result["Trade_Fee"])
            for currency in bfx_wallet:
                self.wallet[currency] = int(
                    bfx_wallet[currency]["Balance"]["value_int"])

            self.signal_wallet(self, None)
            self.ready_info = True
            self.check_connect_ready()

        elif "order_add:" in reqid:
            # order/add has been acked and we got an oid, now we can already
            # insert a pending order into the owns list (it will be pending
            # for a while when the server is busy but the most important thing
            # is that we have the order-id already).
            parts = reqid.split(":")
            typ = parts[1]
            price = int(parts[2])
            volume = int(parts[3])
            oid = result
            self.debug("### got ack for order/add:", typ, price, volume, oid)
            self.count_submitted -= 1
            self.orderbook.add_own(Order(price, volume, typ, oid, "pending"))

        elif "order_cancel:" in reqid:
            # cancel request has been acked but we won't remove it from our
            # own list now because it is still active on the server.
            # do nothing now, let things happen in the user_order message
            parts = reqid.split(":")
            oid = parts[1]
            self.debug("### got ack for order/cancel:", oid)

        else:
            self.debug("### _on_op_result() ignoring:", msg)

    def _on_op_private(self, msg):
        """handle op=private messages, these are the messages of the channels
        we subscribed (trade, depth, ticker) and also the per-account messages
        (user_order, wallet, own trades, etc)"""
        private = msg["private"]
        handler = None
        try:
            handler = getattr(self, "_on_op_private_" + private)
        except AttributeError:
            self.debug("### _on_op_private() ignoring: private=%s" % private)
            self.debug(pretty_format(msg))

        if handler:
            handler(msg)

    def _on_op_private_trade(self, msg):
        """handle incoming trade mesage (op=private, private=trade)"""
        if msg["trade"]["price_currency"] != self.curr_quote:
            return
        if msg["trade"]["item"] != self.curr_base:
            return
        #if msg["channel"] == CHANNELS["trade.%s" % self.curr_base]:
        #    own = False
        #else:
        #    own = True
        own = True
        date = int(msg["trade"]["timestamp"])
        price = float(msg["trade"]["price"])
        volume = float(msg["trade"]["amount"])
        typ = msg["trade"]["trade_type"]

        if own:
            self.debug("trade: %s: %s @ %s (own order filled)" % (
                typ,
                self.base2str(volume),
                self.quote2str(price)
            ))
            # send another private/info request because the fee might have
            # changed. We request it a minute later because the server
            # seems to need some time until the new values are available.
            self.client.request_info_later(60)
        else:
            self.debug("trade: %s: %s @ %s" % (
                typ,
                self.base2str(volume),
                self.quote2str(price)
            ))

        self.signal_trade(self, (date, price, volume, typ, own))

    def _on_op_private_user_order(self, msg):
        """handle incoming user_order message (op=private, private=user_order)"""
        order = msg["user_order"]
        oid = order["oid"]

        # there exist 3 fundamentally different types of user_order messages,
        # they differ in the presence or absence of certain parts of the message

        if "status" in order:
            # these are limit orders or market orders (new or updated).
            #
            # we also need to check whether they belong to our own bfx instance,
            # since they contain currency this is easy, we compare the currency
            # and simply ignore mesages for all unrelated currencies.
            if order["currency"] == self.curr_quote and order["item"] == self.curr_base:
                volume = int(order["amount"]["value_int"])
                typ = order["type"]
                status = order["status"]
                if "price" in order:
                    # these are limit orders (new or updated)
                    price = int(order["price"]["value_int"])
                else:
                    # these are market orders (new or updated)
                    price = 0
                self.signal_userorder(self, (price, volume, typ, oid, status))

        else:
            # these are remove messages (cancel or fill)
            # here it is a bit more expensive to check whether they belong to
            # this bfx instance, they don't carry any other useful data besides
            # the order id and the remove reason but since a remove message can
            # only affect us if the oid is in the owns list already we just
            # ask the orderbook instance whether it knows about this order
            # and ignore all the ones that have unknown oid
            if self.orderbook.have_own_oid(oid):
                # they don't contain a status field either, so we make up
                # our own status string to make it more useful. It will
                # be "removed:" followed by the reason. Possible reasons are:
                # "requested", "completed_passive", "completed_active"
                # so for example a cancel would be "removed:requested"
                # and a limit order fill would be "removed:completed_passive".
                status = "removed:" + order["reason"]
                self.signal_userorder(self, (0, 0, "", oid, status))

    def _on_op_private_wallet(self, msg):
        """handle incoming wallet message (op=private, private=wallet)"""
        balance = msg["wallet"]["balance"]
        currency = balance["currency"]
        total = int(balance["value_int"])
        self.wallet[currency] = total
        self.signal_wallet(self, None)

    def _on_op_remark(self, msg):
        """handler for op=remark messages"""

        if "success" in msg and not msg["success"]:
            if msg["message"] == "Invalid call":
                self._on_invalid_call(msg)
            elif msg["message"] == "Order not found":
                self._on_order_not_found(msg)
            elif msg["message"] == "Order amount is too low":
                self._on_order_amount_too_low(msg)
            elif "Too many orders placed" in msg["message"]:
                self._on_too_many_orders(msg)
            else:
                # we should log this, helps with debugging
                self.debug(msg)

    def _on_invalid_call(self, msg):
        """this comes as an op=remark message and is a strange mystery"""
        # Workaround: Maybe a bug in their server software,
        # I don't know what's missing. Its all poorly documented :-(
        # Sometimes some API calls fail the first time for no reason,
        # if this happens just send them again. This happens only
        # somtimes (10%) and sending them again will eventually succeed.

        if msg["id"] == "idkey":
            self.debug("### resending private/idkey")
            self.client.send_signed_call(
                "private/idkey", {}, "idkey")

        elif msg["id"] == "info":
            self.debug("### resending private/info")
            self.client.send_signed_call(
                "private/info", {}, "info")

        elif msg["id"] == "orders":
            self.debug("### resending private/orders")
            self.client.send_signed_call(
                "private/orders", {}, "orders")

        elif "order_add:" in msg["id"]:
            parts = msg["id"].split(":")
            typ = parts[1]
            price = int(parts[2])
            volume = int(parts[3])
            self.debug("### resending failed", msg["id"])
            self.client.send_order_add(typ, price, volume)

        elif "order_cancel:" in msg["id"]:
            parts = msg["id"].split(":")
            oid = parts[1]
            self.debug("### resending failed", msg["id"])
            self.client.send_order_cancel(oid)

        else:
            self.debug("### _on_invalid_call() ignoring:", msg)

    def _on_order_not_found(self, msg):
        """this means we have sent order/cancel with non-existing oid"""
        parts = msg["id"].split(":")
        oid = parts[1]
        self.debug("### got 'Order not found' for", oid)
        # we are now going to fake a user_order message (the one we
        # obviously missed earlier) that will have the effect of
        # removing the order cleanly.
        fakemsg = {"user_order": {"oid": oid, "reason": "requested"}}
        self._on_op_private_user_order(fakemsg)

    def _on_order_amount_too_low(self, _msg):
        """we received an order_amount too low message."""
        self.debug("### Server said: 'Order amount is too low'")
        self.count_submitted -= 1

    def _on_too_many_orders(self, msg):
        """server complains too many orders were placd too fast"""
        self.debug("### Server said: '%s" % msg["message"])
        self.count_submitted -= 1
        self.signal_order_too_fast(self, msg)


class Level:
    """represents a level in the orderbook"""
    def __init__(self, price, volume):
        self.price = price
        self.volume = volume
        self.own_volume = 0

        # these fields are only used to store temporary cache values
        # in some (not all!) levels and is calculated by the OrderBook
        # on demand, do not access this, use get_total_up_to() instead!
        self._cache_total_vol = 0
        self._cache_total_vol_quote = 0

class Order:
    """represents an order"""
    def __init__(self, price, volume, typ, oid="", status=""):
        """initialize a new order object"""
        self.price = price
        self.volume = volume
        self.typ = typ
        self.oid = oid
        self.status = status

class OrderBook(BaseObject):
    """represents the orderbook. Each Bfx instance has one
    instance of OrderBook to maintain the open orders. This also
    maintains a list of own orders belonging to this account"""

    def __init__(self, bfx):
        """create a new empty orderbook and associate it with its
        Bfx instance, initialize it and connect its slots to bfx"""
        BaseObject.__init__(self)
        self.bfx = bfx

        self.signal_changed             = Signal()
        """orderbook state has changed
        param: None
        an update to the state of the orderbook happened, this is emitted very
        often, it happens after every depth message, after every trade and
        also after every user_order message. This signal is for example used
        in bfxtool.py to repaint the user interface of the orderbook window."""

        self.signal_fulldepth_processed = Signal()
        """fulldepth download is complete
        param: None
        The orderbook (fulldepth) has been downloaded from the server.
        This happens soon after connect."""

        self.signal_owns_initialized    = Signal()
        """own order list has been initialized
        param: None
        The owns list has been initialized. This happens soon after connect
        after it has downloaded the authoritative list of pending and open
        orders. This will also happen if it reinitialized after lost connection."""

        self.signal_owns_changed        = Signal()
        """owns list has changed
        param: None
        an update to the owns list has happened, this can be order added,
        removed or filled, status or volume of an order changed. For specific
        changes to individual orders see the signal_own_* signals below."""

        self.signal_own_added           = Signal()
        """order was added
        param: (order)
        order is a reference to the Order() instance
        This signal will be emitted whenever a new order is added to
        the owns list. Orders will initially have status "pending" and
        some time later there will be signal_own_opened when the status
        changed to open."""

        self.signal_own_removed         = Signal()
        """order has been removed
        param: (order, reason)
        order is a reference to the Order() instance
        reason is a string that can have the following values:
          "requested" order was canceled
          "completed_passive" limit order was filled completely
          "completed_active" market order was filled completely
        Bots will probably be interested in this signal because this is a
        reliable way to determine that a trade has fully completed because the
        trade signal alone won't tell you whether its partial or complete"""

        self.signal_own_opened          = Signal()
        """order status went to "open"
        param: (order)
        order is a reference to the Order() instance
        when the order changes from 'post-pending' to 'open' then this
        signal will be emitted. It won't be emitted for market orders because
        market orders can't have an "open" status, they never move beyond
        "executing", they just execute and emit volume and removed signals."""

        self.signal_own_volume          = Signal()
        """order volume changed (partial fill)
        param: (order, voldiff)
        order is a reference to the Order() instance
        voldiff is the differenc in volume, so for a partial or a complete fill
        it would contain a negative value (integer number of satoshi) of the
        difference between now and the previous volume. This signal is always
        emitted when an order is filled or partially filled, it can be emitted
        multiple times just like the trade messages. It will be emitted for
        all types of orders. The last volume signal that finally brouhgt the
        remaining order volume down to zero will be immediately followed by
        a removed signal."""

        self.bids = [] # list of Level(), lowest ask first
        self.asks = [] # list of Level(), highest bid first
        self.owns = [] # list of Order(), unordered list

        self.bid = 0
        self.ask = 0
        self.total_bid = 0
        self.total_ask = 0

        self.ready_depth = False
        self.ready_owns = False

        self.last_change_type = None # ("bid", "ask", None) this can be used
        self.last_change_price = 0   # for highlighting relative changes
        self.last_change_volume = 0  # of orderbook levels in bfxtool.py

        self._valid_bid_cache = -1   # index of bid with valid _cache_total_vol
        self._valid_ask_cache = -1   # index of ask with valid _cache_total_vol

        bfx.signal_ticker.connect(self.slot_ticker)
        bfx.signal_depth.connect(self.slot_depth)
        bfx.signal_trade.connect(self.slot_trade)
        bfx.signal_userorder.connect(self.slot_user_order)
        bfx.signal_fulldepth.connect(self.slot_fulldepth)

    def slot_ticker(self, dummy_sender, data):
        """Slot for signal_ticker, incoming ticker message"""
        (bid, ask) = data
        self.bid = bid
        self.ask = ask
        self.last_change_type = None
        self.last_change_price = 0
        self.last_change_volume = 0
        self._repair_crossed_asks(ask)
        self._repair_crossed_bids(bid)
        self.signal_changed(self, None)

    def slot_depth(self, dummy_sender, data):
        """Slot for signal_depth, process incoming depth message"""
        (typ, price, _voldiff, total_vol) = data
        if self._update_book(typ, price, total_vol):
            self.signal_changed(self, None)

    def slot_trade(self, dummy_sender, data):
        """Slot for signal_trade event, process incoming trade messages.
        For trades that also affect own orders this will be called twice:
        once during the normal public trade message, affecting the public
        bids and asks and then another time with own=True to update our
        own orders list"""
        (dummy_date, price, volume, typ, own) = data
        if own:
            # nothing special to do here (yet), there will also be
            # separate user_order messages to update my owns list
            # and a copy of this trade message in the public channel
            pass
        else:
            # we update the orderbook. We could also wait for the depth
            # message but we update the orderbook immediately.
            voldiff = -volume
            if typ == "bid":  # tryde_type=bid means an ask order was filled
                self._repair_crossed_asks(price)
                if len(self.asks):
                    if self.asks[0].price == price:
                        self.asks[0].volume -= volume
                        if self.asks[0].volume <= 0:
                            voldiff -= self.asks[0].volume
                            self.asks.pop(0)
                        self.last_change_type = "ask" #the asks have changed
                        self.last_change_price = price
                        self.last_change_volume = voldiff
                        self._update_total_ask(voldiff)
                        self._valid_ask_cache = -1
                if len(self.asks):
                    self.ask = self.asks[0].price

            if typ == "ask":  # trade_type=ask means a bid order was filled
                self._repair_crossed_bids(price)
                if len(self.bids):
                    if self.bids[0].price == price:
                        self.bids[0].volume -= volume
                        if self.bids[0].volume <= 0:
                            voldiff -= self.bids[0].volume
                            self.bids.pop(0)
                        self.last_change_type = "bid" #the bids have changed
                        self.last_change_price = price
                        self.last_change_volume = voldiff
                        self._update_total_bid(voldiff, price)
                        self._valid_bid_cache = -1
                if len(self.bids):
                    self.bid = self.bids[0].price

        self.signal_changed(self, None)

    def slot_user_order(self, dummy_sender, data):
        """Slot for signal_userorder, process incoming user_order mesage"""
        (price, volume, typ, oid, status) = data
        found   = False
        removed = False # was the order removed?
        opened  = False # did the order change from 'post-pending' to 'open'"?
        voldiff = 0     # did the order volume change (full or partial fill)
        if "executing" in status:
            # don't need this status at all
            return
        if "post-pending" in status:
            # don't need this status at all
            return
        if "removed" in status:
            for i in range(len(self.owns)):
                if self.owns[i].oid == oid:
                    order = self.owns[i]

                    # work around MtGox strangeness:
                    # for some reason it will send a "completed_passive"
                    # immediately followed by a "completed_active" when a
                    # market order is filled and removed. Since "completed_passive"
                    # is meant for limit orders only we will just completely
                    # IGNORE all "completed_passive" if it affects a market order,
                    # there WILL follow a "completed_active" immediately after.
                    if order.price == 0:
                        if "passive" in status:
                            # ignore it, the correct one with
                            # "active" will follow soon
                            return

                    self.debug(
                        "### removing order %s " % oid,
                        "price:", self.bfx.quote2str(order.price),
                        "type:", order.typ)

                    # remove it from owns...
                    self.owns.pop(i)

                    # ...and update own volume cache in the bids or asks
                    self._update_level_own_volume(
                        order.typ,
                        order.price,
                        self.get_own_volume_at(order.price, order.typ)
                    )
                    removed = True
                    break
        else:
            for order in self.owns:
                if order.oid == oid:
                    found = True
                    self.debug(
                        "### updating order %s " % oid,
                        "volume:", self.bfx.base2str(volume),
                        "status:", status)
                    voldiff = volume - order.volume
                    opened = (order.status != "open" and status == "open")
                    order.volume = volume
                    order.status = status
                    break

            if not found:
                # This can happen if we added the order with a different
                # application or the bfx server sent the user_order message
                # before the reply to "order/add" (this can happen because
                # actually there is no guarantee which one arrives first).
                # We will treat this like a reply to "order/add"
                self.add_own(Order(price, volume, typ, oid, status))

                # The add_own() method has handled everything that was needed
                # for new orders and also emitted all signals already, we
                # can immediately return here because the job is done.
                return

            # update level own volume cache
            self._update_level_own_volume(
                typ, price, self.get_own_volume_at(price, typ))

        # We try to help the strategy with tracking the orders as good
        # as we can by sending different signals for different events.
        if removed:
            #self.debug(pretty_format(self.bfx.msg))
            #bfx.msg:
            # [
            #     0,
            #     "oc",
            #     [
            #          511019423,
            #          "BTCUSD",
            #          0,
            #          -0.01,
            #          "EXCHANGE LIMIT",
            #          "EXECUTED @ 352.47(-0.01)",  <---status
            #          352.44,
            #          352.47,
            #          "2015-11-27T00:53:14+00:00",
            #          0,
            #          0
            #     ]
            #]

            reason = self.bfx.msg[2][5]
            self.signal_own_removed(self, (order, reason))
        if opened:
            self.signal_own_opened(self, (order))
        if voldiff:
            self.signal_own_volume(self, (order, voldiff))
        self.signal_changed(self, None)
        self.signal_owns_changed(self, None)

    def slot_fulldepth(self, dummy_sender, data):
        """Slot for signal_fulldepth, process received fulldepth data.
        This will clear the book and then re-initialize it from scratch."""
        (depth) = data
        self.debug("### got full depth, updating orderbook...")
        #self.debug(data)
        self.bids = []
        self.asks = []
        self.total_ask = 0
        self.total_bid = 0
        #if "error" in depth:
        #    self.debug("### ", depth["error"])
        #    return
        #self.debug("ASKS:")
        #self.debug(depth["asks"])
        #self.debug("BIDS:")
        #self.debug(depth["bids"])
        #self.debug("FIN")
        for order in depth["asks"]:
            price = float(order["price"])
            volume = float(order["amount"])
            self._update_total_ask(volume)
            self.asks.append(Level(price, volume))
        for order in depth["bids"]:
            price = float(order["price"])
            volume = float(order["amount"])
            self._update_total_bid(volume, price)
            #self.bids.insert(0, Level(price, volume))
            self.bids.append(Level(price, volume))

        # update own volume cache
        for order in self.owns:
            self._update_level_own_volume(
                order.typ, order.price, self.get_own_volume_at(order.price, order.typ))

        if len(self.bids):
            self.bid = self.bids[0].price
        if len(self.asks):
            self.ask = self.asks[0].price

        self._valid_ask_cache = -1
        self._valid_bid_cache = -1
        self.ready_depth = True
        self.signal_fulldepth_processed(self, None)
        self.signal_changed(self, None)

    def _repair_crossed_bids(self, bid):
        """remove all bids that are higher that official current bid value,
        this should actually never be necessary if their feed would not
        eat depth- and trade-messages occaionally :-("""
        while len(self.bids) and self.bids[0].price > bid:
            price = self.bids[0].price
            volume = self.bids[0].volume
            self._update_total_bid(-volume, price)
            self.bids.pop(0)
            self._valid_bid_cache = -1
            #self.debug("### repaired bid")

    def _repair_crossed_asks(self, ask):
        """remove all asks that are lower that official current ask value,
        this should actually never be necessary if their feed would not
        eat depth- and trade-messages occaionally :-("""
        while len(self.asks) and self.asks[0].price < ask:
            volume = self.asks[0].volume
            self._update_total_ask(-volume)
            self.asks.pop(0)
            self._valid_ask_cache = -1
            #self.debug("### repaired ask")

    def _update_book(self, typ, price, total_vol):
        """update the bids or asks list, insert or remove level and
        also update all other stuff that needs to be tracked such as
        total volumes and invalidate the total volume cache index.
        Return True if book has changed, return False otherwise"""
        (lst, index, level) = self._find_level(typ, price)
        if total_vol == 0:
            if level == None:
                return False
            else:
                voldiff = -level.volume
                lst.pop(index)
        else:
            if level == None:
                voldiff = total_vol
                level = Level(price, total_vol)
                lst.insert(index, level)
            else:
                voldiff = total_vol - level.volume
                if voldiff == 0:
                    return False
                level.volume = total_vol

        # now keep all the other stuff in sync with it
        self.last_change_type = typ
        self.last_change_price = price
        self.last_change_volume = voldiff
        if typ == "ask":
            self._update_total_ask(voldiff)
            if len(self.asks):
                self.ask = self.asks[0].price
            self._valid_ask_cache = min(self._valid_ask_cache, index - 1)
        else:
            self._update_total_bid(voldiff, price)
            if len(self.bids):
                self.bid = self.bids[0].price
            self._valid_bid_cache = min(self._valid_bid_cache, index - 1)

        return True

    def _update_total_ask(self, volume):
        """update total volume of base currency on the ask side"""
        self.total_ask += self.bfx.base2float(volume)

    def _update_total_bid(self, volume, price):
        """update total volume of quote currency on the bid side"""
        self.total_bid += \
            self.bfx.base2float(volume) * self.bfx.quote2float(price)

    def _update_level_own_volume(self, typ, price, own_volume):
        """update the own_volume cache in the Level object at price"""

        if price == 0:
            # market orders have price == 0, we don't add them
            # to the orderbook, own_volume is meant for limit orders.
            # Also a price level of 0 makes no sense anyways, this
            # would only insert empty rows at price=0 into the book
            return

        (index, level) = self._find_level_or_insert_new(typ, price)
        if level.volume == 0 and own_volume == 0:
            if typ == "ask":
                self.asks.pop(index)
            else:
                self.bids.pop(index)
        else:
            level.own_volume = own_volume

    def _find_level(self, typ, price):
        """find the level in the orderbook and return a triple
        (list, index, level) where list is a reference to the list,
        index is the index if its an exact match or the index of the next
        element if it was not found (can be used for inserting) and level
        is either a reference to the found level or None if not found."""
        lst = {"ask": self.asks, "bid": self.bids}[typ]
        comp = {"ask": lambda x, y: x < y, "bid": lambda x, y: x > y}[typ]
        low = 0
        high = len(lst)

        # binary search
        while low < high:
            mid = (low + high) // 2
            midval = lst[mid].price
            if comp(midval, price):
                low = mid + 1
            elif comp(price, midval):
                high = mid
            else:
                return (lst, mid, lst[mid])

        # not found, return insertion point (index of next higher level)
        return (lst, high, None)

    def _find_level_or_insert_new(self, typ, price):
        """find the Level() object in bids or asks or insert a new
        Level() at the correct position. Returns tuple (index, level)"""
        (lst, index, level) = self._find_level(typ, price)
        if level:
            return (index, level)

        # no exact match found, create new Level() and insert
        level = Level(price, 0)
        lst.insert(index, level)

        # invalidate the total volume cache at and beyond this level
        if typ == "ask":
            self._valid_ask_cache = min(self._valid_ask_cache, index - 1)
        else:
            self._valid_bid_cache = min(self._valid_bid_cache, index - 1)

        return (index, level)

    def get_own_volume_at(self, price, typ=None):
        """returns the sum of the volume of own orders at a given price. This
        method will not look up the cache in the bids or asks lists, it will
        use the authoritative data from the owns list bacause this method is
        also used to calculate these cached values in the first place."""
        volume = 0
        for order in self.owns:
            if order.price == price and (not typ or typ == order.typ):
                volume += order.volume
        return volume

    def have_own_oid(self, oid):
        """do we have an own order with this oid in our list already?"""
        for order in self.owns:
            if order.oid == oid:
                return True
        return False

    # pylint: disable=W0212
    def get_total_up_to(self, price, is_ask):
        """return a tuple of the total volume in coins and in fiat between top
        and this price. This will calculate the total on demand, it has a cache
        to not repeat the same calculations more often than absolutely needed"""
        if is_ask:
            lst = self.asks
            known_level = self._valid_ask_cache
            comp = lambda x, y: x < y
        else:
            lst = self.bids
            known_level = self._valid_bid_cache
            comp = lambda x, y: x > y

        # now first we need the list index of the level we are looking for or
        # if it doesn't match exactly the index of the level right before that
        # price, for this we do a quick binary search for the price
        low = 0
        high = len(lst)
        while low < high:
            mid = (low + high) // 2
            midval = lst[mid].price
            if comp(midval, price):
                low = mid + 1
            elif comp(price, midval):
                high = mid
            else:
                break
        if comp(price, midval):
            needed_level = mid - 1
        else:
            needed_level = mid

        # if the total volume at this level has been calculated
        # already earlier then we don't need to do anything further,
        # we can immediately return the cached value from that level.
        if needed_level <= known_level:
            lvl = lst[needed_level]
            return (lvl._cache_total_vol, lvl._cache_total_vol_quote)

        # we are still here, this means we must calculate and update
        # all totals in all levels between last_known and needed_level
        # after that is done we can return the total at needed_level.
        if known_level == -1:
            total = 0
            total_quote = 0
        else:
            total = lst[known_level]._cache_total_vol
            total_quote = lst[known_level]._cache_total_vol_quote

        mult_base = self.bfx.mult_base
        for i in range(known_level, needed_level):
            that = lst[i+1]
            total += that.volume
            total_quote += that.volume * that.price / mult_base
            that._cache_total_vol = total
            that._cache_total_vol_quote = total_quote

        if is_ask:
            self._valid_ask_cache = needed_level
        else:
            self._valid_bid_cache = needed_level

        return (total, total_quote)

    def init_own(self, own_orders):
        """called by bfx when the initial order list is downloaded,
        this will happen after connect or reconnect"""
        self.owns = []

        # also reset the own volume cache in bids and ass list
        for level in self.bids + self.asks:
            level.own_volume = 0

        if own_orders:
            for order in own_orders:
                if order["currency"] == self.bfx.curr_quote \
                and order["item"] == self.bfx.curr_base:
                    self._add_own(Order(
                        int(order["price"]["value_int"]),
                        int(order["amount"]["value_int"]),
                        order["type"],
                        order["oid"],
                        order["status"]
                    ))

        self.ready_owns = True
        self.signal_changed(self, None)
        self.signal_owns_initialized(self, None)
        self.signal_owns_changed(self, None)

    def add_own(self, order):
        """called by bfx when a new order has been acked after it has been
        submitted or after a receiving a user_order message for a new order.
        This is a separate method from _add_own because we additionally need
        to fire the a bunch of signals when this happens"""
        if not self.have_own_oid(order.oid):
            self.debug("### adding order:",
                order.typ, order.price, order.volume, order.oid)
            self._add_own(order)
            self.signal_own_added(self, (order))
            self.signal_changed(self, None)
            self.signal_owns_changed(self, None)

    def _add_own(self, order):
        """add order to the list of own orders. This method is used during
        initial download of complete order list."""
        if not self.have_own_oid(order.oid):
            self.owns.append(order)

            # update own volume in that level:
            self._update_level_own_volume(
                order.typ,
                order.price,
                self.get_own_volume_at(order.price, order.typ)
            )
