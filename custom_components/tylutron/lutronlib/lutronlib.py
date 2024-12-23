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

  def _send_locked(self, cmd):
    """Sends the specified command to the lutron controller."""
    _LOGGER.debug("Sending: %s", cmd)
    try:
      self._telnet.write(cmd.encode('ascii') + b'\r\n')
    except _EXPECTED_NETWORK_EXCEPTIONS:
      _LOGGER.exception("Error sending %s", cmd)
      self._disconnect_locked()

  def send(self, cmd):
    """Sends the specified command to the lutron controller.

    Must not hold self._lock.
    """
    with self._lock:
      if not self._connected:
        _LOGGER.debug("Ignoring send of '%s' because we are disconnected", cmd)
        return
      self._send_locked(cmd)

  def _do_login_locked(self):
    """Executes the login procedure (telnet) as well as setting up some
    connection defaults like turning off the prompt, etc."""
    self._telnet = telnetlib.Telnet(self._host, timeout=2)  # 2 second timeout

    # Ensure we know that connection goes away somewhat quickly
    try:
      sock = self._telnet.get_socket()
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      # Some operating systems may not include TCP_KEEPIDLE (macOS, variants of Windows)
      if hasattr(socket, 'TCP_KEEPIDLE'):
        # Send keepalive probes after 60 seconds of inactivity
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
      # Wait 10 seconds for an ACK
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
      # Send 3 probes before we give up
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError:
      _LOGGER.exception('error configuring socket')

    self._telnet.read_until(LutronConnection.USER_PROMPT, timeout=3)
    self._telnet.write(self._user + b'\r\n')
    self._telnet.read_until(LutronConnection.PW_PROMPT, timeout=3)
    self._telnet.write(self._password + b'\r\n')
    self._telnet.read_until(LutronConnection.PROMPT, timeout=3)

    self._send_locked("#MONITORING,12,2")
    self._send_locked("#MONITORING,255,2")
    self._send_locked("#MONITORING,3,1")
    self._send_locked("#MONITORING,4,1")
    self._send_locked("#MONITORING,5,1")
    self._send_locked("#MONITORING,6,1")
    self._send_locked("#MONITORING,8,1")

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
          _LOGGER.debug("Raw line from telnet: %s", line)
        else:
          raise EOFError('Telnet object already torn down')
      except _EXPECTED_NETWORK_EXCEPTIONS:
        _LOGGER.exception("Network exception in main loop")
        try:
          self._lock.acquire()
          self._disconnect_locked()
          # don't spam reconnect
          time.sleep(1)
          continue
        finally:
          self._lock.release()
      self._recv_cb(line.decode('ascii').rstrip())

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


class LutronXmlDbParser(object):
  """The parser for Lutron XML database.

  The database describes all the rooms (Area), keypads (Device), and switches
  (Output). We handle the most relevant features, but some things like LEDs,
  etc. are not implemented."""

  def __init__(self, lutron, xml_db_str):
    """Initializes the XML parser, takes the raw XML data as string input."""
    self._lutron = lutron
    self._xml_db_str = xml_db_str
    self.areas = []
    self._occupancy_groups = {}
    self.project_name = None

  def parse(self):
    """Main entrypoint into the parser. It interprets and creates all the
    relevant Lutron objects and stuffs them into the appropriate hierarchy."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(self._xml_db_str)
    # The structure is something like this:
    # <Areas>
    #   <Area ...>
    #     <DeviceGroups ...>
    #     <Scenes ...>
    #     <ShadeGroups ...>
    #     <Outputs ...>
    #     <Areas ...>
    #       <Area ...>

    # The GUID is unique to the repeater and is useful for constructing unique
    # identifiers that won't change over time.
    self._lutron.set_guid(root.find('GUID').text)

    # Parse Occupancy Groups
    # OccupancyGroups are referenced by entities in the rest of the XML.  The
    # current structure of the code expects to go from areas -> devices ->
    # other assets and attributes.  Here we index the groups to be bound to
    # Areas later.
    groups = root.find('OccupancyGroups')
    for group_xml in groups.iter('OccupancyGroup'):
      group = self._parse_occupancy_group(group_xml)
      if group.group_number:
        self._occupancy_groups[group.group_number] = group
      else:
        _LOGGER.warning("Occupancy Group has no number.  XML: %s", group_xml)

    # First area is useless, it's the top-level project area that defines the
    # "house". It contains the real nested Areas tree, which is the one we want.
    top_area = root.find('Areas').find('Area')
    self.project_name = top_area.get('Name')
    areas = top_area.find('Areas')
    for area_xml in areas.iter('Area'):
      area = self._parse_area(area_xml)
      self.areas.append(area)
    return True

  def _parse_area(self, area_xml):
    """Parses an Area tag, which is effectively a room, depending on how the
    Lutron controller programming was done."""
    occupancy_group_id = area_xml.get('OccupancyGroupAssignedToID')
    occupancy_group = self._occupancy_groups.get(occupancy_group_id)
    area_name = area_xml.get('Name')
    if not occupancy_group:
      _LOGGER.warning("Occupancy Group not found for Area: %s; ID: %s", area_name, occupancy_group_id)
    area = Area(self._lutron,
                name=area_name,
                integration_id=int(area_xml.get('IntegrationID')),
                occupancy_group=occupancy_group)
    for output_xml in area_xml.find('Outputs'):
      output = self._parse_output(output_xml)
      area.add_output(output)
    # device group in our case means keypad
    # device_group.get('Name') is the location of the keypad
    for device_group in area_xml.find('DeviceGroups'):
      if device_group.tag == 'DeviceGroup':
        devs = device_group.find('Devices')
      elif device_group.tag == 'Device':
        devs = [device_group]
      else:
        _LOGGER.info("Unknown tag in DeviceGroups child %s" % devs)
        devs = []
      for device_xml in devs:
        if device_xml.tag != 'Device':
          continue
        if device_xml.get('DeviceType') in (
            'HWI_SEETOUCH_KEYPAD',
            'SEETOUCH_KEYPAD',
            'SEETOUCH_TABLETOP_KEYPAD',
            'PICO_KEYPAD',
            'HYBRID_SEETOUCH_KEYPAD',
            'MAIN_REPEATER',
            'HOMEOWNER_KEYPAD',
            'PALLADIOM_KEYPAD',
            'HWI_SLIM',
            'GRAFIK_T_HYBRID_KEYPAD'):
          keypad = self._parse_keypad(device_xml, device_group)
          area.add_keypad(keypad)
        elif device_xml.get('DeviceType') == 'MOTION_SENSOR':
          motion_sensor = self._parse_motion_sensor(device_xml)
          area.add_sensor(motion_sensor)
        #elif device_xml.get('DeviceType') == 'VISOR_CONTROL_RECEIVER':
    return area

  def _parse_output(self, output_xml):
    """Parses an output, which is generally a switch controlling a set of
    lights/outlets, etc."""
    output_type = output_xml.get('OutputType')
    kwargs = {
        'name': output_xml.get('Name'),
        'watts': int(output_xml.get('Wattage')),
        'output_type': output_type,
        'integration_id': int(output_xml.get('IntegrationID')),
        'uuid': output_xml.get('UUID')
    }
    
    # Create appropriate object based on type
    if output_type == 'SYSTEM_SHADE':
        return Shade(self._lutron, **kwargs)
    elif output_type == 'HVAC':
        return Thermostat(self._lutron, **kwargs)
    else:
        return Output(self._lutron, **kwargs)

  def _parse_keypad(self, keypad_xml, device_group):
    """Parses a keypad device (the Visor receiver is technically a keypad too)."""
    keypad = Keypad(self._lutron,
                    name=keypad_xml.get('Name'),
                    keypad_type=keypad_xml.get('DeviceType'),
                    location=device_group.get('Name'),
                    integration_id=int(keypad_xml.get('IntegrationID')),
                    uuid=keypad_xml.get('UUID'))
    components = keypad_xml.find('Components')
    if components is None:
      return keypad
    for comp in components:
      if comp.tag != 'Component':
        continue
      comp_type = comp.get('ComponentType')
      if comp_type == 'BUTTON':
        button = self._parse_button(keypad, comp)
        keypad.add_button(button)
      elif comp_type == 'LED':
        led = self._parse_led(keypad, comp)
        keypad.add_led(led)
    return keypad

  def _parse_button(self, keypad, component_xml):
    """Parses a button device that part of a keypad."""
    button_xml = component_xml.find('Button')
    name = button_xml.get('Engraving')
    button_type = button_xml.get('ButtonType')
    direction = button_xml.get('Direction')
    # Hybrid keypads have dimmer buttons which have no engravings.
    if button_type == 'SingleSceneRaiseLower':
      name = 'Dimmer ' + direction
    if not name:
      name = "Unknown Button"
    button = Button(self._lutron, keypad,
                    name=name,
                    num=int(component_xml.get('ComponentNumber')),
                    button_type=button_type,
                    direction=direction,
                    uuid=button_xml.get('UUID'))
    return button

  def _parse_led(self, keypad, component_xml):
    """Parses an LED device that part of a keypad."""
    component_num = int(component_xml.get('ComponentNumber'))
    led_base = 80
    if keypad.type == 'MAIN_REPEATER':
      led_base = 100
    led_num = component_num - led_base
    led = Led(self._lutron, keypad,
              name=('LED %d' % led_num),
              led_num=led_num,
              component_num=component_num,
              uuid=component_xml.find('LED').get('UUID'))
    return led

  def _parse_motion_sensor(self, sensor_xml):
    """Parses a motion sensor object.

    TODO: We don't actually do anything with these yet. There's a lot of info
    that needs to be managed to do this right. We'd have to manage the occupancy
    groups, what's assigned to them, and when they go (un)occupied. We'll handle
    this later.
    """
    return MotionSensor(self._lutron,
                        name=sensor_xml.get('Name'),
                        integration_id=int(sensor_xml.get('IntegrationID')),
                        uuid=sensor_xml.get('UUID'))

  def _parse_occupancy_group(self, group_xml):
    """Parses an Occupancy Group object.

    These are defined outside of the areas in the XML.  Areas refer to these
    objects by ID.
    """
    return OccupancyGroup(self._lutron,
                          group_number=group_xml.get('OccupancyGroupNumber'),
                          uuid=group_xml.get('UUID'))

class Lutron(object):
  """Main Lutron Controller class.

  This object owns the connection to the controller, the rooms that exist in the
  network, handles dispatch of incoming status updates, etc.
  """

  # All Lutron commands start with one of these characters
  # See http://www.lutron.com/TechnicalDocumentLibrary/040249.pdf
  OP_EXECUTE = '#'
  OP_QUERY = '?'
  OP_RESPONSE = '~'

  def __init__(self, host, user, password):
    """Initializes the Lutron object. No connection is made to the remote
    device."""
    self._host = host
    self._user = user
    self._password = password
    self._name = None
    self._conn = LutronConnection(host, user, password, self._recv)
    self._ids = {}
    self._legacy_subscribers = {}
    self._areas = []
    self._guid = None

  @property
  def areas(self):
    """Return the areas that were discovered for this Lutron controller."""
    return self._areas

  def set_guid(self, guid):
    self._guid = guid

  @property
  def guid(self):
    return self._guid

  @property
  def name(self):
    return self._name

  def subscribe(self, obj, handler):
    """Subscribes to status updates of the requested object.

    DEPRECATED

    The handler will be invoked when the controller sends a notification
    regarding changed state. The user can then further query the object for the
    state itself."""
    if not isinstance(obj, LutronEntity):
      raise InvalidSubscription("Subscription target not a LutronEntity")
    _LOGGER.warning("DEPRECATED: Subscribing via Lutron.subscribe is obsolete. "
                    "Please use LutronEntity.subscribe")
    if obj not in self._legacy_subscribers:
      self._legacy_subscribers[obj] = handler
      obj.subscribe(self._dispatch_legacy_subscriber, None)

  def register_id(self, cmd_type, obj):
    """Registers an object to receive update notifications."""
    _LOGGER.debug(
        "Registering %s (id=%s) for command type %s",
        obj.name, obj.id, cmd_type
    )
    ids = self._ids.setdefault(cmd_type, {})
    if obj.id in ids:
        raise IntegrationIdExistsError
    self._ids[cmd_type][obj.id] = obj

  def _dispatch_legacy_subscriber(self, obj, *args, **kwargs):
    """This dispatches the registered callback for 'obj'. This is only used
    for legacy subscribers since new users should register with the target
    object directly."""
    if obj in self._legacy_subscribers:
      self._legacy_subscribers[obj](obj)

  def _recv(self, line):
    """Invoked by the connection manager to process incoming data."""
    _LOGGER.debug("Raw line received: %s", line)
    if line == '':
      return
    if line[0] != Lutron.OP_RESPONSE:
      _LOGGER.debug("Ignoring non-response line: %s", line)
      return
      
    parts = line[1:].split(',')
    cmd_type = parts[0]
    integration_id = int(parts[1])
    args = parts[2:]
      
    _LOGGER.debug(
        "Processing command - type: %s, id: %d, args: %s",
        cmd_type, integration_id, args
    )
      
    if cmd_type not in self._ids:
      _LOGGER.warning("Unknown command type %s", cmd_type)
      return
      
    ids = self._ids[cmd_type]
    if integration_id not in ids:
      _LOGGER.warning("Unknown integration id %d for type %s", integration_id, cmd_type)
      return
      
    obj = ids[integration_id]
    _LOGGER.debug("Dispatching to object: %s", obj.name)
    obj.handle_update(args)

  def connect(self):
    """Connects to the Lutron controller to send and receive commands and status"""
    self._conn.connect()

  def send(self, op, cmd, integration_id, *args):
    """Formats and sends the requested command to the Lutron controller."""
    out_cmd = ",".join(
        (cmd, str(integration_id)) + tuple((str(x) for x in args if x is not None)))
    _LOGGER.debug("Sending command to Lutron: %s%s", op, out_cmd)
    self._conn.send(op + out_cmd)

  def load_xml_db(self, cache_path=None):
    """Load the Lutron database from the server.
    
    Args:
        cache_path: Optional path to cache the XML database. If provided and a cached
                   file exists, it will be used instead of querying the server.
                   If the server is queried, the new XML will be cached to this path.
    
    Returns:
        bool: True if successful
    """
    xml_db = None
    loaded_from = None

    if cache_path:
        try:
            with open(cache_path, 'rb') as f:
                xml_db = f.read()
                loaded_from = 'cache'
        except Exception:
            pass

    if not loaded_from:
        import urllib.request
        url = 'http://' + self._host + '/DbXmlInfo.xml'
        with urllib.request.urlopen(url) as xmlfile:
            xml_db = xmlfile.read()
            loaded_from = 'repeater'

    _LOGGER.info("Loaded xml db from %s", loaded_from)

    parser = LutronXmlDbParser(lutron=self, xml_db_str=xml_db)
    assert(parser.parse())     # throw our own exception
    self._areas = parser.areas
    self._name = parser.project_name

    _LOGGER.info("Found Lutron project: %s, %d areas",
        self._name, len(self.areas))

    if cache_path and loaded_from == 'repeater':
        with open(cache_path, 'wb') as f:
            f.write(xml_db)

    return True


class _RequestHelper(object):
  """A class to help with sending queries to the controller and waiting for
  responses.

  It is a wrapper used to help with executing a user action
  and then waiting for an event when that action completes.

  The user calls request() and gets back a threading.Event on which they then
  wait.

  If multiple clients of a lutron object (say an Output) want to get a status
  update on the current brightness (output level), we don't want to spam the
  controller with (near)identical requests. So, if a request is pending, we
  just enqueue another waiter on the pending request and return a new Event
  object. All waiters will be woken up when the reply is received and the
  wait list is cleared.

  NOTE: Only the first enqueued action is executed as the assumption is that the
  queries will be identical in nature.
  """

  def __init__(self):
    """Initialize the request helper class."""
    self.__lock = threading.Lock()
    self.__events = []

  def request(self, action):
    """Request an action to be performed, in case one."""
    ev = threading.Event()
    first = False
    with self.__lock:
      if len(self.__events) == 0:
        first = True
      self.__events.append(ev)
    if first:
      action()
    return ev

  def notify(self):
    with self.__lock:
      events = self.__events
      self.__events = []
    for ev in events:
      ev.set()

# This describes the type signature of the callback that LutronEntity
# subscribers must provide.
LutronEventHandler = Callable[['LutronEntity', Any, 'LutronEvent', Dict], None]


class LutronEvent(Enum):
  """Base class for the events LutronEntity-derived objects can produce."""
  pass


class LutronEntity(object):
  """Base class for all the Lutron objects we'd like to manage. Just holds basic
  common info we'd rather not manage repeatedly."""

  def __init__(self, lutron, name, uuid):
    """Initializes the base class with common, basic data."""
    self._lutron = lutron
    self._name = name
    self._subscribers = []
    self._uuid = uuid

  @property
  def name(self):
    """Returns the entity name (e.g. Pendant)."""
    return self._name

  @property
  def uuid(self):
    return self._uuid

  @property
  def legacy_uuid(self):
    """Return a synthesized uuid."""
    return None

  def _dispatch_event(self, event: LutronEvent, params: Dict):
    """Dispatches the specified event to all the subscribers."""
    _LOGGER.debug(
        "Dispatching event %s with params %s to %d subscribers for %s",
        event, params, len(self._subscribers), self.name
    )
    for handler, context in self._subscribers:
        try:
            handler(self, context, event, params)
        except Exception:
            _LOGGER.exception("Error in event handler")

  def subscribe(self, handler: LutronEventHandler, context) -> Callable[[], None]:
    """Subscribes to events from this entity."""
    _LOGGER.debug(
        "Adding subscription for %s with handler %s and context %s",
        self.name, handler, context
    )
    self._subscribers.append((handler, context))
    return lambda: self._subscribers.remove((handler, context))

  def handle_update(self, args):
    """The handle_update callback is invoked when an event is received
    for the this entity.

    Returns:
      True - If event was valid and was handled.
      False - otherwise.
    """
    return False


class Output(LutronEntity):
  """This is the output entity in Lutron universe. This generally refers to a
  switched/dimmed load, e.g. light fixture, outlet, etc."""
  _CMD_TYPE = 'OUTPUT'
  _ACTION_ZONE_LEVEL = 1
  _ACTION_ZONE_FLASH = 5

  class Event(LutronEvent):
    """Output events that can be generated.

    LEVEL_CHANGED: The output level has changed.
        Params:
          level: new output level (float)
    """
    LEVEL_CHANGED = 1

  def __init__(self, lutron, name, watts, output_type, integration_id, uuid):
    """Initializes the Output."""
    super(Output, self).__init__(lutron, name, uuid)
    self._watts = watts
    self._output_type = output_type
    self._level = 0.0
    self._query_waiters = _RequestHelper()
    self._integration_id = integration_id

    self._lutron.register_id(Output._CMD_TYPE, self)

  def __str__(self):
    """Returns a pretty-printed string for this object."""
    return 'Output name: "%s" watts: %d type: "%s" id: %d' % (
        self._name, self._watts, self._output_type, self._integration_id)

  def __repr__(self):
    """Returns a stringified representation of this object."""
    return str({'name': self._name, 'watts': self._watts,
                'type': self._output_type, 'id': self._integration_id})

  @property
  def id(self):
    """The integration id"""
    return self._integration_id

  @property
  def legacy_uuid(self):
    return '%d-0' % self.id

  def handle_update(self, args):
    """Handles an event update for this object, e.g. dimmer level change."""
    _LOGGER.debug("handle_update %d -- %s" % (self._integration_id, args))
    state = int(args[0])
    if state != Output._ACTION_ZONE_LEVEL:
      return False
    level = float(args[1])
    _LOGGER.debug("Updating %d(%s): s=%d l=%f" % (
        self._integration_id, self._name, state, level))
    self._level = level
    self._query_waiters.notify()
    self._dispatch_event(Output.Event.LEVEL_CHANGED, {'level': self._level})
    return True

  def __do_query_level(self):
    """Helper to perform the actual query the current dimmer level of the
    output. For pure on/off loads the result is either 0.0 or 100.0."""
    self._lutron.send(Lutron.OP_QUERY, Output._CMD_TYPE, self._integration_id,
            Output._ACTION_ZONE_LEVEL)

  def last_level(self):
    """Returns last cached value of the output level, no query is performed."""
    return self._level

  @property
  def level(self):
    """Returns the current output level by querying the remote controller."""
    ev = self._query_waiters.request(self.__do_query_level)
    ev.wait(1.0)
    return self._level

  @level.setter
  def level(self, new_level):
    """Sets the new output level."""
    self.set_level(new_level)

  @staticmethod
  def _fade_time(seconds):
    if seconds is None:
      return None
    return str(timedelta(seconds=seconds))

  def set_level(self, new_level, fade_time_seconds=None):
    """Sets the new output level."""
    if self._level == new_level:
      return
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_ZONE_LEVEL, "%.2f" % new_level, self._fade_time(fade_time_seconds))
    self._level = new_level

  def flash(self, fade_time_seconds=None):
    """Flashes the zone until a new level is set."""
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_ZONE_FLASH, self._fade_time(fade_time_seconds))
    

## At some later date, we may want to also specify delay times
#  def set_level(self, new_level, fade_time_seconds, delay):
#    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE,
#        Output._ACTION_ZONE_LEVEL, new_level, fade_time, delay)

  @property
  def watts(self):
    """Returns the configured maximum wattage for this output (not an actual
    measurement)."""
    return self._watts

  @property
  def type(self):
    """Returns the output type. At present AUTO_DETECT or NON_DIM."""
    return self._output_type

  @property
  def is_dimmable(self):
    """Returns a boolean of whether or not the output is dimmable."""
    return self.type not in ('NON_DIM', 'NON_DIM_INC', 'NON_DIM_ELV', 'EXHAUST_FAN_TYPE', 'RELAY_LIGHTING') and not self.type.startswith('CCO_')


class Shade(Output):
  """This is the output entity for shades in Lutron universe."""
  _ACTION_RAISE = 2
  _ACTION_LOWER = 3
  _ACTION_STOP = 4

  def start_raise(self):
    """Starts raising the shade."""
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_RAISE)

  def start_lower(self):
    """Starts lowering the shade."""
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_LOWER)

  def stop(self):
    """Starts raising the shade."""
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_STOP)


class KeypadComponent(LutronEntity):
  """Base class for a keypad component such as a button, or an LED."""

  def __init__(self, lutron, keypad, name, num, component_num, uuid):
    """Initializes the base keypad component class."""
    super(KeypadComponent, self).__init__(lutron, name, uuid)
    self._keypad = keypad
    self._num = num
    self._component_num = component_num

  @property
  def number(self):
    """Returns the user-friendly number of this component (e.g. Button 1,
    or LED 1."""
    return self._num

  @property
  def component_number(self):
    """Return the lutron component number, which is referenced in commands and
    events. This is different from KeypadComponent.number because this property
    is only used for interfacing with the controller."""
    return self._component_num

  @property
  def legacy_uuid(self):
    return '%d-%d' % (self._keypad.id, self._component_num)

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" Handling "%s" Action: %s Params: %s"' % (
                  self._keypad.name, self.name, action, params))
    return False


class Button(KeypadComponent):
  """This object represents a keypad button that we can trigger and handle
  events for (button presses)."""
  _ACTION_PRESS = 3
  _ACTION_RELEASE = 4
  _ACTION_DOUBLE_CLICK = 6

  class Event(LutronEvent):
    """Button events that can be generated.

    PRESSED: The button has been pressed.
        Params: None

    RELEASED: The button has been released. Not all buttons
              generate this event.
        Params: None

    DOUBLE_CLICKED: The button was double-clicked. Not all buttons
              generate this event.
        Params: None
    """
    PRESSED = 1
    RELEASED = 2
    DOUBLE_CLICKED = 3

  def __init__(self, lutron, keypad, name, num, button_type, direction, uuid):
    """Initializes the Button class."""
    super(Button, self).__init__(lutron, keypad, name, num, num, uuid)
    self._button_type = button_type
    self._direction = direction

  def __str__(self):
    """Pretty printed string value of the Button object."""
    return 'Button name: "%s" num: %d type: "%s" direction: "%s"' % (
        self.name, self.number, self._button_type, self._direction)

  def __repr__(self):
    """String representation of the Button object."""
    return str({'name': self.name, 'num': self.number,
               'type': self._button_type, 'direction': self._direction})

  @property
  def button_type(self):
    """Returns the button type (Toggle, MasterRaiseLower, etc.)."""
    return self._button_type

  def press(self):
    """Triggers a simulated button press to the Keypad."""
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Button._ACTION_PRESS)

  def release(self):
    """Triggers a simulated button release to the Keypad."""
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Button._ACTION_RELEASE)

  def double_click(self):
    """Triggers a simulated button double_click to the Keypad."""
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Button._ACTION_DOUBLE_CLICK)

  def tap(self):
    """Triggers a simulated button tap to the Keypad."""
    self.press()
    self.release()

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" %s Action: %s Params: %s"' % (
                  self._keypad.name, self, action, params))
    ev_map = {
        Button._ACTION_PRESS: Button.Event.PRESSED,
        Button._ACTION_RELEASE: Button.Event.RELEASED,
        Button._ACTION_DOUBLE_CLICK: Button.Event.DOUBLE_CLICKED
    }
    if action not in ev_map:
      _LOGGER.debug("Unknown action %d for button %d in keypad %s" % (
          action, self.number, self._keypad.name))
      return False
    self._dispatch_event(ev_map[action], {})
    return True


class Led(KeypadComponent):
  """This object represents a keypad LED that we can turn on/off and
  handle events for (led toggled by scenes)."""
  _ACTION_LED_STATE = 9

  class Event(LutronEvent):
    """Led events that can be generated.

    STATE_CHANGED: The button has been pressed.
        Params:
          state: The boolean value of the new LED state.
    """
    STATE_CHANGED = 1

  def __init__(self, lutron, keypad, name, led_num, component_num, uuid):
    """Initializes the Keypad LED class."""
    super(Led, self).__init__(lutron, keypad, name, led_num, component_num, uuid)
    self._state = False
    self._query_waiters = _RequestHelper()

  def __str__(self):
    """Pretty printed string value of the Led object."""
    return 'LED keypad: "%s" name: "%s" num: %d component_num: %d"' % (
        self._keypad.name, self.name, self.number, self.component_number)

  def __repr__(self):
    """String representation of the Led object."""
    return str({'keypad': self._keypad, 'name': self.name,
                'num': self.number, 'component_num': self.component_number})

  def __do_query_state(self):
    """Helper to perform the actual query for the current LED state."""
    self._lutron.send(Lutron.OP_QUERY, Keypad._CMD_TYPE, self._keypad.id,
            self.component_number, Led._ACTION_LED_STATE)

  @property
  def last_state(self):
    """Returns last cached value of the LED state, no query is performed."""
    return self._state

  @property
  def state(self):
    """Returns the current LED state by querying the remote controller."""
    ev = self._query_waiters.request(self.__do_query_state)
    ev.wait(1.0)
    return self._state

  @state.setter
  def state(self, new_state: bool):
    """Sets the new led state.

    new_state: bool
    """
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Led._ACTION_LED_STATE,
                      int(new_state))
    self._state = new_state

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" %s Action: %s Params: %s"' % (
                  self._keypad.name, self, action, params))
    if action != Led._ACTION_LED_STATE:
      _LOGGER.debug("Unknown action %d for led %d in keypad %s" % (
          action, self.number, self._keypad.name))
      return False
    elif len(params) < 1:
      _LOGGER.debug("Unknown params %s (action %d on led %d in keypad %s)" % (
          params, action, self.number, self._keypad.name))
      return False
    self._state = bool(params[0])
    self._query_waiters.notify()
    self._dispatch_event(Led.Event.STATE_CHANGED, {'state': self._state})
    return True


class Keypad(LutronEntity):
  """Object representing a Lutron keypad.

  Currently we don't really do much with it except handle the events
  (and drop them on the floor).
  """
  _CMD_TYPE = 'DEVICE'

  def __init__(self, lutron, name, keypad_type, location, integration_id, uuid):
    """Initializes the Keypad object."""
    super(Keypad, self).__init__(lutron, name, uuid)
    self._buttons = []
    self._leds = []
    self._components = {}
    self._location = location
    self._integration_id = integration_id
    self._type = keypad_type

    self._lutron.register_id(Keypad._CMD_TYPE, self)

  def add_button(self, button):
    """Adds a button that's part of this keypad. We'll use this to
    dispatch button events."""
    self._buttons.append(button)
    self._components[button.component_number] = button

  def add_led(self, led):
    """Add an LED that's part of this keypad."""
    self._leds.append(led)
    self._components[led.component_number] = led

  @property
  def id(self):
    """The integration id"""
    return self._integration_id

  @property
  def legacy_uuid(self):
    return '%d-0' % self.id

  @property
  def name(self):
    """Returns the name of this keypad"""
    return self._name

  @property
  def type(self):
    """Returns the keypad type"""
    return self._type

  @property
  def location(self):
    """Returns the location in which the keypad is installed"""
    return self._location

  @property
  def buttons(self):
    """Return a tuple of buttons for this keypad."""
    return tuple(button for button in self._buttons)

  @property
  def leds(self):
    """Return a tuple of leds for this keypad."""
    return tuple(led for led in self._leds)

  def handle_update(self, args):
    """The callback invoked by the main event loop if there's an event from this keypad."""
    component = int(args[0])
    action = int(args[1])
    params = [int(x) for x in args[2:]]
    _LOGGER.debug("Updating %d(%s): c=%d a=%d params=%s" % (
        self._integration_id, self._name, component, action, params))
    if component in self._components:
      return self._components[component].handle_update(action, params)
    return False


class PowerSource(Enum):
  """Enum values representing power source, reported by queries to
  battery-powered devices."""
  
  # Values from ?HELP,?DEVICE,22
  UNKNOWN = 0
  BATTERY = 1
  EXTERNAL = 2

  
class BatteryStatus(Enum):
  """Enum values representing battery state, reported by queries to
  battery-powered devices."""
  
  # Values from ?HELP,?DEVICE,22 don't match the documentation, using what's in the doc.
  #?HELP says:
  # <0-NOT BATTERY POWERED, 1-DEVICE_BATTERY_STATUS_UNKNOWN, 2-DEVICE_BATTERY_STATUS_GOOD, 3-DEVICE_BATTERY_STATUS_LOW, 4-DEVICE_STATUS_MIA>5-DEVICE_STATUS_NOT_ACTIVATED>
  NORMAL = 1
  LOW = 2
  OTHER = 3  # not sure what this value means


class MotionSensor(LutronEntity):
  """Placeholder class for the motion sensor device.
  Although sensors are represented in the XML, all of the protocol
  happens at the OccupancyGroup level. To read the state of an area,
  use area.occupancy_group.
  """

  _CMD_TYPE = 'DEVICE'

  _ACTION_BATTERY_STATUS = 22

  class Event(LutronEvent):
    """MotionSensor events that can be generated.
    STATUS_CHANGED: Battery status changed
        Params:
          power: PowerSource
          battery: BatteryStatus
    Note that motion events are reported by OccupancyGroup, not individual
    MotionSensors.
    """
    STATUS_CHANGED = 1

  def __init__(self, lutron, name, integration_id, uuid):
    """Initializes the motion sensor object."""
    super(MotionSensor, self).__init__(lutron, name, uuid)
    self._integration_id = integration_id
    self._battery = None
    self._power = None
    self._lutron.register_id(MotionSensor._CMD_TYPE, self)
    self._query_waiters = _RequestHelper()
    self._last_update = None

  @property
  def id(self):
    """The integration id"""
    return self._integration_id

  @property
  def legacy_uuid(self):
    return str(self.id)

  def __str__(self):
    """Returns a pretty-printed string for this object."""
    return 'MotionSensor {} Id: {} Battery: {} Power: {}'.format(
        self.name, self.id, self.battery_status, self.power_source)

  def __repr__(self):
    """String representation of the MotionSensor object."""
    return str({'motion_sensor_name': self.name, 'id': self.id,
                'battery' : self.battery_status,
                'power' : self.power_source})

  @property
  def _update_age(self):
    """Returns the time of the last poll in seconds."""
    if self._last_update is None:
      return 1e6
    else:
      return time.time() - self._last_update

  @property
  def battery_status(self):
    """Returns the current BatteryStatus."""
    # Battery status won't change frequently but can't be retrieved for MONITORING.
    # So rate limit queries to once an hour.
    if self._update_age > 3600.0:
      ev = self._query_waiters.request(self._do_query_battery)
      ev.wait(1.0)
    return self._battery

  @property
  def power_source(self):
    """Returns the current PowerSource."""
    self.battery_status  # retrieved by the same query
    return self._power

  def _do_query_battery(self):
    """Helper to perform the query for the current BatteryStatus."""
    component_num = 1  # doesn't seem to matter
    return self._lutron.send(Lutron.OP_QUERY, MotionSensor._CMD_TYPE, self._integration_id,
                             component_num, MotionSensor._ACTION_BATTERY_STATUS)

  def handle_update(self, args):
    """Handle the specified action on this component."""
    if len(args) != 6:
      _LOGGER.debug('Wrong number of args for MotionSensor update {}'.format(len(args)))
      return False
    _, action, _, power, battery, _ = args
    action = int(action)
    if action != MotionSensor._ACTION_BATTERY_STATUS:
      _LOGGER.debug("Unknown action %d for motion sensor {}".format(self.name))
      return False
    self._power = PowerSource(int(power))
    self._battery = BatteryStatus(int(battery))
    self._last_update = time.time()
    self._query_waiters.notify()
    self._dispatch_event(
      MotionSensor.Event.STATUS_CHANGED, {'power' : self._power, 'battery': self._battery})
    return True


class OccupancyGroup(LutronEntity):
  """Represents one or more occupancy/vacancy sensors grouped into an Area."""
  _CMD_TYPE = 'GROUP'
  _ACTION_STATE = 3

  class State(Enum):
    """Possible states of an OccupancyGroup."""
    OCCUPIED = 3
    VACANT = 4
    UNKNOWN = 255

  class Event(LutronEvent):
    """OccupancyGroup event that can be generated.
    OCCUPANCY: Occupancy state has changed.
        Params:
          state: an OccupancyGroup.State
    """
    OCCUPANCY = 1

  def __init__(self, lutron, group_number, uuid):
    super(OccupancyGroup, self).__init__(lutron, None, uuid)
    self._area = None
    self._group_number = group_number
    self._integration_id = None
    self._state = None
    self._query_waiters = _RequestHelper()

  def _bind_area(self, area):
    self._area = area
    self._integration_id = area.id
    if self._integration_id != 0:
      self._lutron.register_id(OccupancyGroup._CMD_TYPE, self)

  @property
  def id(self):
    """The integration id, which is the area's integration_id"""
    return self._integration_id

  @property
  def legacy_uuid(self):
    return '%s-%s' % (self._area.id, self._group_number)

  @property
  def group_number(self):
    """The OccupancyGroupNumber"""
    return self._group_number

  @property
  def name(self):
    """Return the name of this OccupancyGroup, which is 'Occ' plus the name of the area."""
    return 'Occ {}'.format(self._area.name)

  @property
  def state(self):
    """Returns the current occupancy state."""
    # Poll for the first request.
    if self._state == None:
      ev = self._query_waiters.request(self._do_query_state)
      ev.wait(1.0)
    return self._state

  def __str__(self):
    """Returns a pretty-printed string for this object."""
    return 'OccupancyGroup for Area "{}" Id: {} State: {}'.format(
        self._area.name, self.id, self.state.name)

  def __repr__(self):
    """Returns a stringified representation of this object."""
    return str({'area_name' : self.area.name,
                'id' : self.id,
                'state' : self.state})

  def _do_query_state(self):
    """Helper to perform the actual query for the current OccupancyGroup state."""
    return self._lutron.send(Lutron.OP_QUERY, OccupancyGroup._CMD_TYPE, self._integration_id,
                             OccupancyGroup._ACTION_STATE)


  def handle_update(self, args):
    """Handles an event update for this object, e.g. occupancy state change."""
    action = int(args[0])
    if action != OccupancyGroup._ACTION_STATE or len(args) != 2:
      return False
    try:
      self._state = OccupancyGroup.State(int(args[1]))
    except ValueError:
      self._state = OccupancyGroup.State.UNKNOWN
    self._query_waiters.notify()
    self._dispatch_event(OccupancyGroup.Event.OCCUPANCY, {'state': self._state})
    return True


class Area(object):
  """An area (i.e. a room) that contains devices/outputs/etc."""
  def __init__(self, lutron, name, integration_id, occupancy_group):
    self._lutron = lutron
    self._name = name
    self._integration_id = integration_id
    self._occupancy_group = occupancy_group
    self._outputs = []
    self._keypads = []
    self._sensors = []
    self._thermostats = []
    if occupancy_group:
      occupancy_group._bind_area(self)

  def add_output(self, output):
    """Adds an output object that's part of this area, only used during
    initial parsing."""
    self._outputs.append(output)
    # If this is a thermostat (HVAC output), also add it to thermostats list
    if isinstance(output, Thermostat):
      self._thermostats.append(output)

  def add_keypad(self, keypad):
    """Adds a keypad object that's part of this area, only used during
    initial parsing."""
    self._keypads.append(keypad)

  def add_sensor(self, sensor):
    """Adds a motion sensor object that's part of this area, only used during
    initial parsing."""
    self._sensors.append(sensor)

  def add_thermostat(self, thermostat):
    """Adds a thermostat object that's part of this area."""
    self._thermostats.append(thermostat)

  @property
  def name(self):
    """Returns the name of this area."""
    return self._name

  @property
  def id(self):
    """The integration id of the area."""
    return self._integration_id

  @property
  def occupancy_group(self):
    """Returns the OccupancyGroup for this area, or None."""
    return self._occupancy_group

  @property
  def outputs(self):
    """Return the tuple of the Outputs from this area."""
    return tuple(output for output in self._outputs)

  @property
  def keypads(self):
    """Return the tuple of the Keypads from this area."""
    return tuple(keypad for keypad in self._keypads)

  @property
  def sensors(self):
    """Return the tuple of the MotionSensors from this area."""
    return tuple(sensor for sensor in self._sensors)

  @property
  def thermostats(self):
    """Return the tuple of Thermostats from this area."""
    return tuple(thermostat for thermostat in self._thermostats)

class ThermostatMode(Enum):
    """Operating modes for a Lutron thermostat."""
    OFF = 1          # Off/Protect
    HEAT = 2
    COOL = 3
    AUTO = 4
    EMERGENCY_HEAT = 5  # Emergency Heat/Auxiliary Only
    LOCKED_OUT = 6      # RCS HVAC controller locked out
    FAN = 7
    DRY = 8

class ThermostatFanMode(Enum):
    """Fan modes for a Lutron thermostat."""
    AUTO = 1
    ON = 2
    CYCLER = 3
    NO_FAN = 4
    HIGH = 5
    MEDIUM = 6
    LOW = 7
    TOP = 8

class ThermostatCallStatus(Enum):
    """HVAC call status values."""
    NONE_LAST_HEAT = 0
    HEAT_STAGE_1 = 1
    HEAT_STAGE_1_2 = 2
    NONE_LAST_COOL = 3  # Add if supported
    COOL_STAGE_1 = 4    # Add if supported
    COOL_STAGE_2 = 5    # Add if supported

class ThermostatSensorStatus(Enum):
    """Temperature sensor connection status values."""
    ALL_ACTIVE = 1
    MISSING_SENSOR = 2
    WIRED_ONLY = 3
    NO_SENSOR = 4

class ThermostatScheduleMode(Enum):
    """Schedule modes for a Lutron thermostat."""
    UNAVAILABLE = 0  # Schedule not programmed or device needs date/time
    FOLLOWING = 1    # Running programmed schedule
    PERMANENT_HOLD = 2  # Schedule not running
    TEMPORARY_HOLD = 3  # Running schedule, returns to schedule at next event

class ThermostatSystemMode(Enum):
    """System modes for a Lutron thermostat."""
    NORMAL = 1
    AWAY = 2
    EMERGENCY = 3

class Thermostat(LutronEntity):
    """Object representing a Lutron thermostat."""
    _CMD_TYPE = 'HVAC'
    
    class Event(LutronEvent):
        """Thermostat events that can be generated."""
        TEMPERATURE_CHANGED = auto()
        SETPOINTS_CHANGED = auto()
        MODE_CHANGED = auto()
        FANMODE_CHANGED = auto()
        ECO_MODE_CHANGED = auto()
        SCHEDULE_MODE_CHANGED = auto()
        SYSTEM_MODE_CHANGED = auto()
        SENSOR_STATUS_CHANGED = auto()
        CALL_STATUS_CHANGED = auto()
    
    # Define action codes for different thermostat commands
    _ACTION_TEMP_F = 1             # Temperature in Fahrenheit
    _ACTION_SETPOINTS_F = 2        # Heat and Cool setpoints in Fahrenheit
    _ACTION_MODE = 3               # Operating mode
    _ACTION_FANMODE = 4            # Fan mode
    _ACTION_ECO_MODE = 5           # Eco (Setback) mode
    _ACTION_ECO_OFFSET = 6         # Eco offset (read-only)
    _ACTION_SCHEDULE_STATUS = 7     # Schedule status
    _ACTION_SENSOR_STATUS = 8       # Temperature sensor connection status
    _ACTION_SCHEDULE_EVENT = 9      # Schedule event details
    _ACTION_SCHEDULE_DAY = 10       # Schedule day assignment
    _ACTION_SYSTEM_MODE = 11        # System mode
    _ACTION_SETPOINTS_NO_ECO_F = 12 # Setpoints without eco offset in Fahrenheit
    _ACTION_EMERGENCY_AVAIL = 13    # Emergency heat availability
    _ACTION_CALL_STATUS = 14        # HVAC call status
    _ACTION_TEMP_C = 15            # Temperature in Celsius
    _ACTION_SETPOINTS_C = 16       # Heat and Cool setpoints in Celsius
    _ACTION_SETPOINTS_NO_ECO_C = 17 # Setpoints without eco offset in Celsius

    def __init__(self, lutron, **kwargs):
        """Initialize the thermostat object.
        
        Args:
            lutron: The main Lutron controller object
            **kwargs: Keyword arguments including:
                name: Name of the thermostat
                integration_id: Integration ID from the Lutron system
                uuid: UUID from the Lutron system
                use_celsius: Whether to use Celsius commands (True) or Fahrenheit (False)
        """
        super(Thermostat, self).__init__(lutron, kwargs['name'], kwargs['uuid'])
        self._integration_id = kwargs['integration_id']
        self._use_celsius = kwargs.get('use_celsius', False)
        
        # Temperature and setpoint actions depend on temperature scale
        self._temp_action = self._ACTION_TEMP_C if self._use_celsius else self._ACTION_TEMP_F
        self._setpoints_action = self._ACTION_SETPOINTS_C if self._use_celsius else self._ACTION_SETPOINTS_F
        self._setpoints_no_eco_action = self._ACTION_SETPOINTS_NO_ECO_C if self._use_celsius else self._ACTION_SETPOINTS_NO_ECO_F
        
        # Initialize state variables
        self._temperature = None
        self._heat_setpoint = None
        self._cool_setpoint = None
        self._mode = None
        self._fan_mode = None
        self._eco_mode = None
        self._eco_offset = None
        self._schedule_mode = None
        self._system_mode = None
        self._call_status = None
        self._emergency_heat_available = None
        
        # Create query waiters for each value we can request
        self._temperature_query = _RequestHelper()
        self._setpoints_query = _RequestHelper()
        self._mode_query = _RequestHelper()
        self._fan_mode_query = _RequestHelper()
        self._eco_mode_query = _RequestHelper()
        self._eco_offset_query = _RequestHelper()
        self._schedule_mode_query = _RequestHelper()
        self._system_mode_query = _RequestHelper()
        self._call_status_query = _RequestHelper()
        self._emergency_heat_query = _RequestHelper()
        
        _LOGGER.debug(
            "Registering thermostat %s with integration_id %s for command type %s",
            self.name, self._integration_id, self._CMD_TYPE
        )
        self._lutron.register_id(self._CMD_TYPE, self)

    @property
    def id(self):
        """The integration id"""
        return self._integration_id

    @property
    def legacy_uuid(self):
        """Legacy uuid property"""
        return str(self.id)

    # Basic properties
    @property
    def temperature(self):
        """Get the current temperature."""
        if self._temperature is None:
            ev = self._temperature_query.request(self._query_temperature)
            ev.wait(1.0)
        return self._temperature

    @property
    def mode(self):
        """Get the current operating mode."""
        if self._mode is None:
            ev = self._mode_query.request(self._query_mode)
            ev.wait(1.0)
        return self._mode

    @property
    def fan_mode(self):
        """Get the current fan mode."""
        if self._fan_mode is None:
            ev = self._fan_mode_query.request(self._query_fan_mode)
            ev.wait(1.0)
        return self._fan_mode

    # Add these properties after the other basic properties
    @property
    def heat_setpoint(self):
        """Get the current heat setpoint."""
        if self._heat_setpoint is None:
            ev = self._setpoints_query.request(self._query_setpoints)
            ev.wait(1.0)
        return self._heat_setpoint

    @property
    def cool_setpoint(self):
        """Get the current cool setpoint."""
        if self._cool_setpoint is None:
            ev = self._setpoints_query.request(self._query_setpoints)
            ev.wait(1.0)
        return self._cool_setpoint

    # Setters
    def set_mode(self, mode):
        """Set the operating mode."""
        _LOGGER.debug(
            "Setting mode for thermostat %s to: %s",
            self.name, mode
        )
        if not isinstance(mode, ThermostatMode):
            raise ValueError("Mode must be a ThermostatMode enum value")
        self._lutron.send(Lutron.OP_EXECUTE, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_MODE,
                         mode.value)
        self._mode = mode

    def set_fan_mode(self, mode):
        """Set the fan mode."""
        _LOGGER.debug(
            "Setting fan mode for thermostat %s to: %s",
            self.name, mode
        )
        if not isinstance(mode, ThermostatFanMode):
            raise ValueError("Mode must be a ThermostatFanMode enum value")
        self._lutron.send(Lutron.OP_EXECUTE, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_FANMODE,
                         mode.value)
        self._fan_mode = mode

    def set_setpoints(self, heat_setpoint=None, cool_setpoint=None):
        """Set heat and/or cool setpoints."""
        _LOGGER.debug(
            "Setting setpoints for thermostat %s - Heat: %s, Cool: %s",
            self.name, heat_setpoint, cool_setpoint
        )
        heat_val = "%.1f" % heat_setpoint if heat_setpoint is not None else "255"
        cool_val = "%.1f" % cool_setpoint if cool_setpoint is not None else "255"
        
        self._lutron.send(Lutron.OP_EXECUTE, Thermostat._CMD_TYPE,
                         self._integration_id, self._setpoints_action,
                         heat_val, cool_val)
        
        if heat_setpoint is not None:
            self._heat_setpoint = heat_setpoint
        if cool_setpoint is not None:
            self._cool_setpoint = cool_setpoint

    # Query methods
    def _query_temperature(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._temp_action)

    def _query_mode(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_MODE)

    def _query_fan_mode(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_FANMODE)

    def handle_update(self, args):
        """Handle status updates from the thermostat."""
        try:
            action = int(args[0])
            # Temperature updates
            if action in (self._ACTION_TEMP_F, self._ACTION_TEMP_C):
                value = self._parse_temp(args[1])
                self._temperature = value
                self._temperature_query.notify()
                self._dispatch_event(Thermostat.Event.TEMPERATURE_CHANGED, 
                                   {'temperature': value})
            
            # Setpoint updates
            elif action in (self._ACTION_SETPOINTS_F, self._ACTION_SETPOINTS_C,
                           self._ACTION_SETPOINTS_NO_ECO_F, self._ACTION_SETPOINTS_NO_ECO_C):
                if len(args) < 3:
                    return False
                heat = self._parse_temp(args[1])
                cool = self._parse_temp(args[2])
                self._heat_setpoint = heat
                self._cool_setpoint = cool
                self._setpoints_query.notify()
                self._dispatch_event(Thermostat.Event.SETPOINTS_CHANGED,
                                   {'heat': heat, 'cool': cool})
            
            # Mode updates    
            elif action == self._ACTION_MODE:
                self._mode = ThermostatMode(int(args[1]))
                self._mode_query.notify()
                self._dispatch_event(Thermostat.Event.MODE_CHANGED,
                                   {'mode': self._mode})
            
            # Fan mode updates
            elif action == self._ACTION_FANMODE:
                self._fan_mode = ThermostatFanMode(int(args[1]))
                self._fan_mode_query.notify()
                self._dispatch_event(Thermostat.Event.FANMODE_CHANGED,
                                   {'mode': self._fan_mode})

            # Eco mode updates
            elif action == self._ACTION_ECO_MODE:
                self._eco_mode = int(args[1]) == 2  # 1=Off, 2=On
                self._eco_mode_query.notify()
                self._dispatch_event(Thermostat.Event.ECO_MODE_CHANGED,
                                   {'enabled': self._eco_mode})
            
            elif action == self._ACTION_ECO_OFFSET:
                self._eco_offset = float(args[1])
                self._eco_offset_query.notify()
            
            elif action == self._ACTION_SCHEDULE_STATUS:
                self._schedule_mode = ThermostatScheduleMode(int(args[1]))
                self._schedule_mode_query.notify()
                self._dispatch_event(Thermostat.Event.SCHEDULE_MODE_CHANGED,
                                   {'mode': self._schedule_mode})

            # Sensor status updates
            elif action == self._ACTION_SENSOR_STATUS:
                self._sensor_status = ThermostatSensorStatus(int(args[1]))
                self._sensor_status_query.notify()
                self._dispatch_event(Thermostat.Event.SENSOR_STATUS_CHANGED,
                                   {'status': self._sensor_status})
            
            # System mode updates
            elif action == self._ACTION_SYSTEM_MODE:
                self._system_mode = ThermostatSystemMode(int(args[1]))
                self._system_mode_query.notify()
                self._dispatch_event(Thermostat.Event.SYSTEM_MODE_CHANGED,
                                   {'mode': self._system_mode})
            
            # Call status updates
            elif action == self._ACTION_CALL_STATUS:
                self._call_status = ThermostatCallStatus(int(args[1]))
                self._call_status_query.notify()
                self._dispatch_event(Thermostat.Event.CALL_STATUS_CHANGED,
                                   {'status': self._call_status})

            elif action == self._ACTION_EMERGENCY_AVAIL:
                self._emergency_heat_available = int(args[1]) == 1
                self._emergency_heat_query.notify()
            
            else:
                return False
            
            return True
            
        except (ValueError, IndexError):
            _LOGGER.warning(f"Invalid thermostat update: {args}")
            return False

    def _parse_temp(self, temp_str):
        """Parse temperature values that may be zero-padded with varying decimal places."""
        # Strip any leading zeros while preserving decimal point
        temp_str = temp_str.lstrip('0')
        if temp_str.startswith('.'):
            temp_str = '0' + temp_str
        return float(temp_str)

    # Query helper methods
    def _query_eco_mode(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_ECO_MODE)

    def _query_eco_offset(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_ECO_OFFSET)

    def _query_schedule_mode(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_SCHEDULE_STATUS)

    def _query_sensor_status(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_SENSOR_STATUS)

    def _query_system_mode(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_SYSTEM_MODE)

    def _query_call_status(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_CALL_STATUS)

    def _query_emergency_heat_available(self):
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_EMERGENCY_AVAIL)

    # Properties for the new features
    @property
    def sensor_status(self):
        """Get the temperature sensor connection status."""
        if self._sensor_status is None:
            ev = self._sensor_status_query.request(self._query_sensor_status)
            ev.wait(1.0)
        return self._sensor_status

    @property
    def call_status(self):
        """Get the current HVAC call status."""
        if self._call_status is None:
            ev = self._call_status_query.request(self._query_call_status)
            ev.wait(1.0)
        return self._call_status

    @property
    def emergency_heat_available(self):
        """Check if emergency heat is available."""
        if self._emergency_heat_available is None:
            ev = self._emergency_heat_query.request(self._query_emergency_heat_available)
            ev.wait(1.0)
        return self._emergency_heat_available

    def get_schedule_event(self, schedule_num, event_num):
        """Get details for a specific schedule event.
        
        Args:
            schedule_num: Schedule number (1-7)
            event_num: Event number (1-4)
        
        Returns:
            Dict with event details (time, heat setpoint, cool setpoint)
        """
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_SCHEDULE_EVENT,
                         schedule_num, event_num)
        # Note: Response handling would need to be implemented in handle_update

    def get_schedule_days(self, schedule_num):
        """Get the days assigned to a specific schedule.
        
        Args:
            schedule_num: Schedule number (1-7)
            
        Returns:
            List of days (0=Sunday through 6=Saturday) when this schedule is active
        """
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_SCHEDULE_DAY,
                         schedule_num)
        # Note: Response handling would need to be implemented in handle_update

    # And add this query method with the other query methods
    def _query_setpoints(self):
        """Query both heat and cool setpoints."""
        self._lutron.send(Lutron.OP_QUERY, Thermostat._CMD_TYPE,
                         self._integration_id, self._setpoints_action)

    @property
    def eco_mode(self):
        """Get the current eco mode status."""
        if self._eco_mode is None:
            ev = self._eco_mode_query.request(self._query_eco_mode)
            ev.wait(1.0)
        return self._eco_mode

    @property
    def eco_offset(self):
        """Get the current eco offset value."""
        if self._eco_offset is None:
            ev = self._eco_offset_query.request(self._query_eco_offset)
            ev.wait(1.0)
        return self._eco_offset

    @property
    def schedule_mode(self):
        """Get the current schedule mode."""
        if self._schedule_mode is None:
            ev = self._schedule_mode_query.request(self._query_schedule_mode)
            ev.wait(1.0)
        return self._schedule_mode

    @property
    def system_mode(self):
        """Get the current system mode."""
        if self._system_mode is None:
            ev = self._system_mode_query.request(self._query_system_mode)
            ev.wait(1.0)
        return self._system_mode

    def set_eco_mode(self, enabled: bool) -> None:
        """Set eco mode on or off."""
        self._lutron.send(Lutron.OP_EXECUTE, Thermostat._CMD_TYPE,
                         self._integration_id, self._ACTION_ECO_MODE,
                         2 if enabled else 1)  # 1=Off, 2=On

__all__ = [
    'Lutron',
    'Button',
    'Output',
    'Shade',
    'Thermostat',
    'ThermostatMode',
    'ThermostatFanMode',
    'ThermostatScheduleMode',
    'ThermostatSystemMode',
    'ThermostatCallStatus',
    'ThermostatSensorStatus',
    'Area',
    'Keypad',
    'Led',
    'MotionSensor',
    'OccupancyGroup'
]
