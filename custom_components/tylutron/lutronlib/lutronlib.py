"""
Lutron RadioRA 2 module for interacting with the Main Repeater. Basic operations
for enumerating and controlling the loads are supported.

"""

__author__ = "Dima Zavin"
__copyright__ = "Copyright 2016, Dima Zavin"

from datetime import timedelta
from enum import Enum, auto
import logging
import socket
import threading
import time

from ._telnetlib import telnetlib
from typing import Any, Callable, Dict, Type

_LOGGER = logging.getLogger(__name__)

# We brute force exception handling in a number of areas to ensure
# connections can be recovered
_EXPECTED_NETWORK_EXCEPTIONS = (
  BrokenPipeError,
  # OSError: [Errno 101] Network unreachable
  OSError,
  EOFError,
  TimeoutError,
  socket.timeout,
)

class LutronException(Exception):
  """Top level module exception."""
  pass


class IntegrationIdExistsError(LutronException):
  """Asserted when there's an attempt to register a duplicate integration id."""
  pass


class ConnectionExistsError(LutronException):
  """Raised when a connection already exists (e.g. user calls connect() twice)."""
  pass


class InvalidSubscription(LutronException):
  """Raised when an invalid subscription is requested (e.g. calling
  Lutron.subscribe on an incompatible object."""
  pass


class LutronConnection(threading.Thread):
  """Encapsulates the connection to the Lutron controller."""
  USER_PROMPT = b'login: '
  PW_PROMPT = b'password: '
  PROMPT = b'GNET> '

  def __init__(self, host, user, password, recv_callback):
    """Initializes the lutron connection, doesn't actually connect."""
    threading.Thread.__init__(self)

    self._host = host
    self._user = user.encode('ascii')
    self._password = password.encode('ascii')
    self._telnet = None
    self._connected = False
    self._lock = threading.Lock()
    self._connect_cond = threading.Condition(lock=self._lock)
    self._recv_cb = recv_callback
    self._done = False

    self.setDaemon(True)

  def connect(self):
    """Connects to the lutron controller."""
    if self._connected or self.is_alive():
      raise ConnectionExistsError("Already connected")
    # After starting the thread we wait for it to post us
    # an event signifying that connection is established. This
    # ensures that the caller only resumes when we are fully connected.
    self.start()
    with self._lock:
      self._connect_cond.wait_for(lambda: self._connected)

  def send(self, cmd):
    """Sends the specified command to the lutron controller.

    Must not hold self._lock.
    """
    with self._lock:
      if not self._connected:
        _LOGGER.debug("Ignoring send of '%s' because we are disconnected", cmd)
        return
      _LOGGER.debug("Sending raw command: %s", cmd)
      self._send_locked(cmd)

  def _send_locked(self, cmd):
    """Sends the specified command to the lutron controller."""
    _LOGGER.debug("Sending to telnet: %s", cmd)
    try:
      self._telnet.write(cmd.encode('ascii') + b'\r\n')
      response = self._telnet.read_until(b'\n', timeout=1)
      _LOGGER.debug("Command response: %s", response)
    except _EXPECTED_NETWORK_EXCEPTIONS:
      _LOGGER.exception("Error sending %s", cmd)
      self._disconnect_locked()

  def _do_login_locked(self):
    """Executes the login procedure and sets up monitoring."""
    try:
      self._telnet = telnetlib.Telnet(self._host, timeout=2)
      
      # Configure socket
      sock = self._telnet.get_socket()
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      if hasattr(socket, 'TCP_KEEPIDLE'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

      # Login sequence
      _LOGGER.debug("Starting login sequence...")
      self._telnet.read_until(LutronConnection.USER_PROMPT, timeout=3)
      self._telnet.write(self._user + b'\r\n')
      self._telnet.read_until(LutronConnection.PW_PROMPT, timeout=3)
      self._telnet.write(self._password + b'\r\n')
      self._telnet.read_until(LutronConnection.PROMPT, timeout=3)
      _LOGGER.debug("Login successful, setting up monitoring...")

      # Enable monitoring for all relevant updates
      self._send_locked("#MONITORING,12,2")  # Enable detailed monitoring
      self._send_locked("#MONITORING,255,2") # Enable all device types
      self._send_locked("#MONITORING,3,1")   # Enable temperature updates
      self._send_locked("#MONITORING,4,1")   # Enable setpoint updates
      self._send_locked("#MONITORING,5,1")   # Enable mode updates
      self._send_locked("#MONITORING,6,1")   # Enable fan updates
      self._send_locked("#MONITORING,8,1")   # Enable status updates
      _LOGGER.info("Monitoring enabled successfully")

    except Exception as e:
      _LOGGER.exception("Error during login sequence")
      raise

  def _disconnect_locked(self):
    """Closes the current connection. Assume self._lock is held."""
    was_connected = self._connected
    self._connected = False
    self._connect_cond.notify_all()
    self._telnet = None
    if was_connected:
      _LOGGER.warning("Disconnected")

  def _maybe_reconnect(self):
    """Reconnects to the controller if we have been previously disconnected."""
    with self._lock:
      if not self._connected:
        _LOGGER.info("Connecting")
        # This can throw an exception, but we'll catch it in run()
        self._do_login_locked()
        self._connected = True
        self._connect_cond.notify_all()
        _LOGGER.info("Connected")

  def _main_loop(self):
    """Main body of the thread function."""
    while True:
      line = b''
      try:
        self._maybe_reconnect()
        t = self._telnet
        if t is not None:
          line = t.read_until(b"\n", timeout=3)
          if line and line not in (b'\n', b'\r\n', b''):
            _LOGGER.debug("Raw line from telnet: %s", line)
            decoded = line.decode('ascii').rstrip()
            _LOGGER.debug("Decoded line: %s", decoded)
            if decoded.startswith('~'):  # Only process response lines
              self._recv_cb(decoded)
        else:
          raise EOFError('Telnet object already torn down')
      except _EXPECTED_NETWORK_EXCEPTIONS:
        _LOGGER.exception("Network exception in main loop")
        try:
          self._lock.acquire()
          self._disconnect_locked()
        finally:
          self._lock.release()
        time.sleep(5)  # Wait before reconnecting

  def run(self):
    """Main entry point into our receive thread.

    It just wraps _main_loop() so we can catch exceptions.
    """
    _LOGGER.info("Started")
    try:
      self._main_loop()
    except Exception:
      _LOGGER.exception("Uncaught exception")
      raise


[... rest of the file remains unchanged ...]
