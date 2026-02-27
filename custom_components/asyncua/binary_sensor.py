"""Platform for sensor integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AsyncuaCoordinator
from .const import (
    CONF_NODE_DEVICE_CLASS,
    CONF_NODE_HUB,
    CONF_NODE_ID,
    CONF_NODE_NAME,
    CONF_NODE_UNIQUE_ID,
    CONF_NODES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

NODE_SCHEMA = {
    CONF_NODES: [
        {
            vol.Optional(CONF_NODE_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_NODE_UNIQUE_ID): cv.string,
            vol.Required(CONF_NODE_ID): cv.string,
            vol.Required(CONF_NODE_NAME): cv.string,
            vol.Required(CONF_NODE_HUB): cv.string,
        }
    ]
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    schema=NODE_SCHEMA,
    extra=vol.ALLOW_EXTRA,
)

"""
데이터 타입 불일치 문제나 서버 측에서 **Int(0, 1)**로 데이터를 보내는 상황을 고려할 때, 데이터 해석의 유연성을 높이는 수정이 필요합니다.

아르헨티나 현장의 BESS 시스템에서 통신 두절이나 예기치 않은 데이터 값이 들어와도 센서가 unknown으로 뻗지 않도록 보완된 코드를 제안
"""

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up asyncua_binary_sensor coordinator_nodes."""

    coordinator_nodes: dict[str, list[dict[str, str]]] = {}
    coordinators: dict[str, AsyncuaCoordinator] = {}
    asyncua_sensors: list = []

    try:
        # 허브별 노드 분류
        for _idx_node, val_node in enumerate(config[CONF_NODES]):
            if val_node[CONF_NODE_HUB] not in coordinator_nodes:
                coordinator_nodes[val_node[CONF_NODE_HUB]] = []
            coordinator_nodes[val_node[CONF_NODE_HUB]].append(val_node)

        # 코디네이터 생성 및 센서 객체 리스트화
        for key_coordinator, val_coordinator in coordinator_nodes.items():
            if key_coordinator not in hass.data[DOMAIN]:
                raise ConfigEntryError(
                    f"Asyncua hub {key_coordinator} not found. Specify a valid asyncua hub in the configuration."
                )
            coordinators[key_coordinator] = hass.data[DOMAIN][key_coordinator]
            coordinators[key_coordinator].add_sensors(sensors=val_coordinator)

            for _idx_sensor, val_sensor in enumerate(val_coordinator):
                asyncua_sensors.append(
                    AsyncuaBinarySensor(
                        coordinator=coordinators[key_coordinator],
                        name=val_sensor[CONF_NODE_NAME],
                        unique_id=val_sensor.get(CONF_NODE_UNIQUE_ID),
                        hub=val_sensor[CONF_NODE_HUB],
                        node_id=val_sensor[CONF_NODE_ID],
                        device_class=val_sensor.get(CONF_NODE_DEVICE_CLASS),
                    )
                )

        # 엔티티 등록
        async_add_entities(new_entities=asyncua_sensors)

    except Exception as critical_err:
        _LOGGER.critical("Critical error during binary_sensor setup: %s", critical_err)


class AsyncuaBinarySensor(CoordinatorEntity[AsyncuaCoordinator], BinarySensorEntity):
    """A binary sensor implementation for Asyncua OPCUA nodes."""

    def __init__(
        self,
        coordinator: AsyncuaCoordinator,
        name: str,
        hub: str,
        node_id: str,
        device_class: Any,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator=coordinator)
        self._attr_name = name
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{DOMAIN}.{hub}.{node_id}"
        )
        self._attr_available = False
        self._attr_device_class = device_class
        self._attr_is_on: bool | None = None
        self._hub = hub
        self._node_id = node_id

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        try:
            raw_value = self._parse_coordinator_data(
                coordinator_data=self.coordinator.data
            )

            # 🚨 [개선 1] 데이터 유효성 검사 및 가용성 상태 업데이트
            if raw_value is None:
                self._attr_available = False
                return None

            self._attr_available = True

            # 🚨 [개선 2] 다양한 데이터 타입(Int, String, Bool)을 안전하게 Boolean으로 변환
            if isinstance(raw_value, str):
                # 문자열 "0", "false", "off" (대소문자 무시)는 False로 처리
                self._attr_is_on = raw_value.lower() not in ("0", "false", "off", "no", "")
            else:
                # 숫자 0은 False, 그 외(1 등)는 True로 처리
                self._attr_is_on = bool(raw_value)

            return self._attr_is_on

        except Exception as err:
            _LOGGER.error("Error determining state for binary sensor %s: %s", self._attr_name, err)
            self._attr_available = False
            return None

    @property
    def unique_id(self) -> str | None:
        """Return the unique_id of the sensor."""
        return self._attr_unique_id

    @property
    def node_id(self) -> str:
        """Return the node address provided by the OPCUA server."""
        return self._node_id

    def _parse_coordinator_data(
            self,
            coordinator_data: dict[str, Any],
    ) -> Any:
        """Parse the value from the mapped coordinator."""
        try:
            # 🚨 [개선 3] 에러 방지: 데이터가 아예 없는 경우 안전하게 None 반환
            if coordinator_data is None:
                return None

            if self._attr_name is None:
                return None

            # name 기반으로 데이터를 찾되, 없으면 None 반환
            return coordinator_data.get(self._attr_name)
        except Exception as err:
            _LOGGER.debug("Data parsing error in %s: %s", self._attr_name, err)
            return None