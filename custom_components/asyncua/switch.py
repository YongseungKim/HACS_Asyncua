"""Platform for switch integration."""

from __future__ import annotations

import logging
import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE, STATE_OK
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AsyncuaCoordinator
from .const import (
    CONF_NODE_HUB,
    CONF_NODE_ID,
    CONF_NODE_NAME,
    CONF_NODE_SWITCH_DI,
    CONF_NODE_UNIQUE_ID,
    CONF_NODES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

NODE_SCHEMA = {
    CONF_NODES: [
        {
            vol.Required(CONF_NODE_HUB): cv.string,
            vol.Required(CONF_NODE_NAME): cv.string,
            vol.Required(CONF_NODE_ID): cv.string,
            vol.Optional(CONF_NODE_SWITCH_DI): cv.string,
            vol.Optional(CONF_NODE_UNIQUE_ID): cv.string,
        }
    ]
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    schema=NODE_SCHEMA,
    extra=vol.ALLOW_EXTRA,
)

"""
제공해주신 switch.py 소스 코드는 전체적인 제어 로직은 잘 구성되어 있으나, 
앞서 발생한 센서 데이터(String) 변환 오류가 스위치 초기화 과정을 중단시키는 구조적 취약점을 가지고 있습니다.

로그에서 보셨듯이 스위치가 로드될 때 await val_switch.async_init() → await self.coordinator.async_refresh()를 호출하는데, 
이때 센서 쪽에 문자열 에러가 있으면 스위치도 함께 뻗어버립니다. 이를 방지하기 위한 보완 코드를 제안합니다.
"""

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up asyncua_switch coordinator_nodes."""

    coordinator_nodes: dict[str, list[dict[str, str]]] = {}
    coordinators: dict[str, AsyncuaCoordinator] = {}
    asyncua_switches: list = []

    try:
        for _idx_node, val_node in enumerate(config[CONF_NODES]):
            if val_node[CONF_NODE_HUB] not in coordinator_nodes:
                coordinator_nodes[val_node[CONF_NODE_HUB]] = []
            coordinator_nodes[val_node[CONF_NODE_HUB]].append(val_node)

        for key_coordinator, val_coordinator in coordinator_nodes.items():
            # Get the respective asyncua coordinator
            if key_coordinator not in hass.data[DOMAIN]:
                raise ConfigEntryError(
                    f"Asyncua hub {key_coordinator} not found. Specify a valid asyncua hub in the configuration."
                )
            coordinators[key_coordinator] = hass.data[DOMAIN][key_coordinator]
            coordinators[key_coordinator].add_sensors(sensors=val_coordinator)

            for _idx_sensor, val_sensor in enumerate(val_coordinator):
                asyncua_switches.append(
                    AsyncuaSwitch(
                        coordinator=coordinators[key_coordinator],
                        name=val_sensor[CONF_NODE_NAME],
                        hub=val_sensor[CONF_NODE_HUB],
                        node_id=val_sensor[CONF_NODE_ID],
                        addr_di=val_sensor.get(CONF_NODE_SWITCH_DI),
                        unique_id=val_sensor.get(CONF_NODE_UNIQUE_ID),
                    )
                )

        async_add_entities(asyncua_switches)

        # 🚨 스위치 개별 초기화 시 오류가 전체를 중단시키지 않도록 방어
        for idx_switch, val_switch in enumerate(asyncua_switches):
            try:
                await val_switch.async_init()
                _LOGGER.debug("Initialized switch %s - %s", idx_switch, val_switch.attr_name)
            except Exception as err:
                _LOGGER.error("Failed to initialize switch %s: %s", val_switch.attr_name, err)

    except Exception as critical_err:
        _LOGGER.critical("Critical error during asyncua switch setup: %s", critical_err)


class AsyncuaSwitch(SwitchEntity, CoordinatorEntity[AsyncuaCoordinator]):
    """A switch implementation for Asyncua OPCUA nodes."""

    def __init__(
        self,
        coordinator: AsyncuaCoordinator,
        name: str,
        hub: str,
        node_id: str,
        addr_di: str | None = None,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator=coordinator)
        self._attr_name = name
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{DOMAIN}.{hub}.{node_id}"
        )
        # 초기 가용성 상태 설정
        self._attr_available = False
        self._available = False
        self._attr_device_class = SwitchDeviceClass.SWITCH
        self._attr_is_on = None
        self._hub = hub
        self._coordinator = coordinator
        self._node_id = node_id
        self._addr_di = addr_di if addr_di is not None else node_id

    @property
    def attr_name(self):
        """Return __attr_name variable."""
        return self._attr_name

    @property
    def is_on(self) -> bool | None:
        """Check if OPCUA connection is available and return state."""
        try:
            if not self.coordinator.hub.connected:
                self._attr_is_on = None
                self._attr_available = False
                return None

            # 코디네이터 캐시에서 값을 가져옴 (안전한 접근)
            raw_val = self.coordinator.hub.cache_val.get(self._attr_unique_id, None)

            if raw_val is None:
                self._attr_is_on = None
            else:
                # Int(0, 1)와 Bool(True, False) 모두를 안전하게 처리
                self._attr_is_on = bool(raw_val)

            self._attr_available = True
            return self._attr_is_on
        except Exception as err:
            _LOGGER.error("Error reading state for %s: %s", self._attr_name, err)
            return None

    async def async_init(self) -> None:
        """Initialize switch to get latest value."""
        try:
            await self._async_set_value()
        except Exception as err:
            _LOGGER.warning("Initial value fetch failed for %s (Server might be starting): %s", self._attr_name, err)

    async def _async_set_value(self, val: bool = None, **kwargs) -> None:
        """Set value and handle potential errors during refresh."""
        try:
            if val is not None:
                # 서버 전송 (실패 시 예외 발생)
                await self.coordinator.hub.set_value(
                    nodeid=self._node_id,
                    value=val,
                    **kwargs,
                )

            # 피드백 값을 읽어옴 (제어 주소와 상태 주소가 다를 수 있음)
            new_val = await self.coordinator.hub.get_value(nodeid=self._addr_di)

            if new_val is not None:
                self._attr_is_on = bool(new_val)
                self._attr_available = True

            # 리프레시 중 다른 센서 에러가 스위치 제어를 방해하지 않도록 독립 처리
            try:
                await self.coordinator.async_refresh()
            except Exception as e:
                _LOGGER.warning("Refresh post-toggle failed for %s, but command was sent: %s", self._attr_name, e)

        except Exception as err:
            _LOGGER.error("Error setting switch value for %s: %s", self._attr_name, err)
            self._attr_available = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._async_set_value(val=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._async_set_value(val=False)