"""Support for Curtain and Airer."""
import logging
import time
from enum import Enum
from functools import partial

from homeassistant.const import *
from homeassistant.core import callback
from homeassistant.components.cover import (
    CoverEntity,
    SUPPORT_OPEN,
    SUPPORT_CLOSE,
    SUPPORT_STOP,
    SUPPORT_SET_POSITION,
    DEVICE_CLASS_CURTAIN,
    DEVICE_CLASS_DAMPER,
    ATTR_POSITION,
)
from homeassistant.components.fan import SUPPORT_SET_SPEED
from homeassistant.helpers.event import async_track_utc_time_change

from . import (
    DOMAIN,
    CONF_MODEL,
    PLATFORM_SCHEMA,
    MiioEntity,
    MiotEntity,
    MiioDevice,
    MiotDevice,
    DeviceException,
    bind_services_to_entries,
)
from .light import LightSubEntity
from .fan import FanSubEntity

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'cover.{DOMAIN}'

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = hass.data[DOMAIN]['configs'].get(config_entry.entry_id, dict(config_entry.data))
    await async_setup_platform(hass, config, async_add_entities)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    config.setdefault('add_entities', {})
    config['add_entities']['cover'] = async_add_entities
    model = str(config.get(CONF_MODEL) or '')
    entities = []
    if model.find('mrbond.airer') >= 0:
        entity = MrBondAirerProEntity(config)
        entities.append(entity)
    elif model.find('airer') >= 0:
        entity = MijiaAirerEntity(config)
        entities.append(entity)
    elif model.find('curtain') >= 0:
        entity = LumiCurtainEntity(config)
        entities.append(entity)
    for entity in entities:
        hass.data[DOMAIN]['entities'][entity.unique_id] = entity
    async_add_entities(entities, update_before_add=True)
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class MiioCoverEntity(MiioEntity, CoverEntity):
    def __init__(self, name, device):
        super().__init__(name, device)
        self._device_class = None
        self._position = None
        self._set_position = None
        self._unsub_listener_cover = None
        self._is_opening = False
        self._is_closing = False
        self._closed = False
        self._requested_closing = True

    @property
    def current_cover_position(self):
        return self._position

    @property
    def is_closed(self):
        return self._closed

    @property
    def is_closing(self):
        return self._is_closing

    @property
    def is_opening(self):
        return self._is_opening

    @property
    def device_class(self):
        return self._device_class

    def open_cover(self, **kwargs):
        pass

    def close_cover(self, **kwargs):
        pass

    @callback
    def _listen_cover(self):
        if self._unsub_listener_cover is None:
            self._unsub_listener_cover = async_track_utc_time_change(
                self.hass, self._time_changed_cover
            )

    async def _time_changed_cover(self, now):
        if self._requested_closing:
            self._position -= 10 if self._position >= 10 else 0
        else:
            self._position += 10 if self._position <= 90 else 0
        if self._position in (100, 0, self._set_position):
            self._unsub_listener_cover()
            self._unsub_listener_cover = None
            self._set_position = None
        self._closed = self.current_cover_position <= 1
        self.async_write_ha_state()
        _LOGGER.debug('cover process %s: %s', self.name, {
            'position': self._position,
            'set_position': self._set_position,
            'requested_closing': self._requested_closing,
        })


class LumiCurtainEntity(MiioCoverEntity, MiotEntity, CoverEntity):
    mapping = {
        # http://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:curtain:0000A00C:lumi-hagl05:1
        'motor_control':    {'siid': 2, 'piid': 2},  # 0:Pause 1:Open 2:Close 3:auto, writeOnly
        'current_position': {'siid': 2, 'piid': 3},  # [0, 100], step 1
        'status':           {'siid': 2, 'piid': 6},  # 0:Stopped 1:Opening 2:Closing
        'target_position':  {'siid': 2, 'piid': 7},  # [0, 100], step 1
        'manual_enabled':   {'siid': 4, 'piid': 1},  # 0:Disable 1:Enable
        'polarity':         {'siid': 4, 'piid': 2},  # 0:Positive 1:Reverse
        'pos_limit':        {'siid': 4, 'piid': 3},  # 0:Unlimit 1:Limit
        'night_tip_light':  {'siid': 4, 'piid': 4},  # 0:Disable 1:Enable
        'run_time':         {'siid': 4, 'piid': 5},  # [0, 255], step 1
        'adjust_value':     {'siid': 5, 'piid': 1},  # [-100, 100], step 1, writeOnly
    }

    def __init__(self, config):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        self._device = MiotDevice(self.mapping, host, token)
        super().__init__(name, self._device)
        self._device_class = DEVICE_CLASS_CURTAIN
        self._supported_features = SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP | SUPPORT_SET_POSITION
        self._state_attrs.update({'entity_class': self.__class__.__name__})

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            self._position = round(attrs.get('current_position', -1))
            self._is_opening = int(attrs.get('status', 0)) == 1
            self._is_closing = int(attrs.get('status', 0)) == 2
            self._closed = self._position <= 0
            self._state_attrs.update({
                'position': self._position,
                'closed':   self._closed,
                'stopped':  bool(not self._is_opening and not self._is_closing),
            })
            if self._unsub_listener_cover is not None and self._state_attrs['stopped']:
                self._unsub_listener_cover()
                self._unsub_listener_cover = None

    def open_cover(self, **kwargs):
        return self._device.set_property('motor_control', 1)

    async def async_open_cover(self, **kwargs):
        if self._position is None or self._position >= 99:
            return
        if await self.async_set_property('motor_control', 1):
            self._state_attrs['status'] = 1
            self._is_opening = True
            self._requested_closing = False
            self._set_position = 100
            self._listen_cover()
            self.async_write_ha_state()

    def close_cover(self, **kwargs):
        return self._device.set_property('motor_control', 2)

    async def async_close_cover(self, **kwargs):
        if self.is_closed or self._position is None:
            return
        if await self.async_set_property('motor_control', 2):
            self._state_attrs['status'] = 2
            self._is_closing = True
            self._requested_closing = True
            self._set_position = 0
            self._listen_cover()
            self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs):
        position = kwargs.get(ATTR_POSITION)
        self._set_position = round(position, -1)
        if self._position == position:
            return
        if await self.async_set_property('target_position', self._set_position):
            self._listen_cover()
            self._requested_closing = position < self._position

    async def async_stop_cover(self, **kwargs):
        if self._position is None:
            return
        if await self.async_set_property('motor_control', 0):
            self._is_closing = False
            self._is_opening = False


class MijiaAirerEntity(MiotEntity, MiioCoverEntity):
    mapping = {
        # http://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:airer:0000A00D:hyd-znlyj1:1
        'fault':            {'siid': 2, 'piid': 1},  #
        'motor_control':    {'siid': 2, 'piid': 2},  # 0:Pause 1:Up 2:Down, writeOnly
        'current_position': {'siid': 2, 'piid': 3},  # [0, 2], step 1
        'status':           {'siid': 2, 'piid': 4},  # 0:Stopped 1:Up 2:Down 3:Pause
        'light':            {'siid': 3, 'piid': 1},  # bool
    }

    def __init__(self, config):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        self._device = MiotDevice(self.mapping, host, token)
        self._add_entities = config.get('add_entities') or {}
        super().__init__(name, self._device)
        self._device_class = DEVICE_CLASS_DAMPER
        self._supported_features = SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP
        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._subs = {}

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            self._position = 100 - round(attrs.get('current_position') or 0, -1) * 50
            self._is_opening = int(attrs.get('status') or 0) == 1
            self._is_closing = int(attrs.get('status') or 0) == 2
            self._closed = self._position <= 0
            self._state_attrs.update({
                'stopped': bool(not self._is_opening and not self._is_closing),
            })

            add_lights = self._add_entities.get('light', None)
            if 'light' in self._subs:
                self._subs['light'].update()
            elif add_lights and 'light' in attrs:
                self._subs['light'] = LightSubEntity(self, 'light')
                add_lights([self._subs['light']])

    def open_cover(self, **kwargs):
        return self._device.set_property('motor_control', 1)

    async def async_open_cover(self, **kwargs):
        if await self.async_set_property('motor_control', 1):
            self._state_attrs['status'] = 1
            self._is_opening = True
            self._set_position = 100

    def close_cover(self, **kwargs):
        return self._device.set_property('motor_control', 2)

    async def async_close_cover(self, **kwargs):
        if await self.async_set_property('motor_control', 2):
            self._state_attrs['status'] = 2
            self._is_closing = True
            self._set_position = 0

    async def async_stop_cover(self, **kwargs):
        if await self.async_set_property('motor_control', 0):
            self._is_closing = False
            self._is_opening = False

    def turn_on_light(self):
        if self._device.set_property('light', True):
            self._state_attrs['light'] = True

    def turn_off_light(self):
        if self._device.set_property('light', False):
            self._state_attrs['light'] = False


class MrBondAirerProEntity(MiioCoverEntity):
    def __init__(self, config):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        self._device = MiioDevice(host, token)
        self._add_entities = config.get('add_entities')
        super().__init__(name, self._device)
        self._device_class = DEVICE_CLASS_DAMPER
        self._supported_features = SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP
        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._props = ['dry', 'led', 'motor', 'drytime', 'airer_location']
        self._subs = {}

    def get_single_prop(self, prop):
        rls = self._device.get_properties([prop]) or []
        return rls[0]

    async def async_get_single_prop(self, prop):
        return await self.hass.async_add_executor_job(partial(self.get_single_prop, prop))

    async def async_update(self):
        attrs = []
        try:
            attrs = await self.hass.async_add_executor_job(
                partial(self._device.send, 'get_prop', self._props, extra_parameters={
                    'id': int(time.time() % 86400 * 1000),
                })
            )
            self._available = True
        except DeviceException as ex:
            err = '%s' % ex
            if err.find('-10000') > 0:
                # Unknown Error: {'code': -10000, 'message': 'error'}
                try:
                    attrs = [
                        self.get_single_prop('dry'),
                        self.get_single_prop('led'),
                        self.get_single_prop('motor'),
                        None,
                        None,
                    ]
                    self._available = True
                except DeviceException as exc:
                    if self._available:
                        self._available = False
                    _LOGGER.error(
                        'Got exception while fetching the state for %s (%s): %s %s',
                        self._name, self._props, ex, exc
                    )
            else:
                _LOGGER.error(
                    'Got exception while fetching the state for %s (%s): %s',
                    self._name, self._props, ex
                )
        if self._available:
            attrs = dict(zip(self._props, attrs))
            _LOGGER.debug('Got new state from %s: %s', self.name, attrs)
            self._state_attrs.update(attrs)
            self._position = 100 if int(attrs.get('airer_location', 1) or 1) else 0
            self._is_opening = int(attrs.get('motor', 0)) == 1
            self._is_closing = int(attrs.get('motor', 0)) == 2
            if attrs.get('airer_location', None) is None:
                if self._is_opening:
                    self._position = 100
                if self._is_closing:
                    self._position = 0
            self._closed = self._position <= 0
            self._state_attrs.update({
                'position': self._position,
                'closed':   self._closed,
                'stopped':  bool(not self._is_opening and not self._is_closing),
            })
            if self._unsub_listener_cover is not None and self._state_attrs['stopped']:
                self._unsub_listener_cover()
                self._unsub_listener_cover = None

            add_lights = self._add_entities.get('light', None)
            if 'light' in self._subs:
                self._subs['light'].update()
            elif add_lights and 'led' in attrs:
                self._subs['light'] = MrBondAirerProLightEntity(self)
                add_lights([self._subs['light']])

            add_fans = self._add_entities.get('fan', None)
            if 'fan' in self._subs:
                self._subs['fan'].update()
            elif add_fans and 'dry' in attrs:
                self._subs['fan'] = MrBondAirerProDryEntity(self, option={'keys': ['drytime']})
                add_fans([self._subs['fan']])

    def open_cover(self, **kwargs):
        self._device.send('set_motor', [1])

    async def async_open_cover(self, **kwargs):
        if self._position is None or self._position >= 100:
            return
        if await self.async_command('set_motor', [1]):
            self._is_opening = True
            self._listen_cover()
            self._requested_closing = False
            self.async_write_ha_state()

    def close_cover(self, **kwargs):
        self._device.send('set_motor', [2])

    async def async_close_cover(self, **kwargs):
        if self._position == 0 or self.is_closed:
            return
        if await self.async_command('set_motor', [2]):
            self._is_closing = True
            self._listen_cover()
            self._requested_closing = True
            self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs):
        if self._position is None:
            return
        if await self.async_command('set_motor', [0]):
            self._is_closing = False
            self._is_opening = False

    @property
    def icon(self):
        return 'mdi:hanger'


class MrBondAirerProLightEntity(LightSubEntity):
    def __init__(self, parent: MrBondAirerProEntity, attr='led', option=None):
        super().__init__(parent, attr, option)

    def update(self):
        super().update()
        if self._available:
            attrs = self._state_attrs
            self._state = int(attrs.get(self._attr, 0)) >= 1

    def turn_on(self, **kwargs):
        if self.call_parent('send_command', 'set_led', [1]):
            self._state = True
            self.update_attrs({self._attr: 1}, True)

    def turn_off(self, **kwargs):
        if self.call_parent('send_command', 'set_led', [0]):
            self._state = False
            self.update_attrs({self._attr: 0}, True)


class MrBondAirerProDryEntity(FanSubEntity):
    def __init__(self, parent: MrBondAirerProEntity, attr='dry', option=None):
        super().__init__(parent, attr, option)
        self._supported_features = SUPPORT_SET_SPEED

    def update(self):
        super().update()
        if self._available:
            attrs = self._state_attrs
            self._state = int(attrs.get(self._attr, 0)) >= 1

    def turn_on(self, speed, **kwargs):
        return self.set_speed(speed)

    def turn_off(self, **kwargs):
        return self.set_speed(MrBondAirerProDryLevels(0).name)

    @property
    def speed(self):
        return MrBondAirerProDryLevels(int(self._state_attrs.get(self._attr, 0))).name

    @property
    def speed_list(self):
        return [v.name for v in MrBondAirerProDryLevels]

    def set_speed(self, speed: str):
        lvl = MrBondAirerProDryLevels[speed].value
        if lvl == 0:
            ret = self.call_parent('send_command', 'set_dryswitch', [0])
        elif lvl >= 4:
            ret = self.call_parent('send_command', 'set_dryswitch', [1])
        else:
            ret = self.call_parent('send_command', 'set_dry', [lvl])
        if ret:
            self._state = lvl >= 1
            self.update_attrs({self._attr: lvl}, True)
        return ret


class MrBondAirerProDryLevels(Enum):
    Off = 0
    Dry30Minutes = 1
    Dry60Minutes = 2
    Dry90Minutes = 3
    Dry120Minutes = 4
