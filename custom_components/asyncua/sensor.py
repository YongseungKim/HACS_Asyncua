"""Platform for sensor integration."""
from __future__ import annotations

import logging
from typing import Any, Union

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
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
    CONF_NODE_STATE_CLASS,
    CONF_NODE_UNIQUE_ID,
    CONF_NODE_UNIT_OF_MEASUREMENT,
    CONF_NODES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

NODE_SCHEMA = {
    CONF_NODES: [
        {
            vol.Optional(CONF_NODE_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_NODE_STATE_CLASS, default="measurement"): cv.string,
            vol.Optional(CONF_NODE_UNIT_OF_MEASUREMENT): cv.string,
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
데이터가 숫자가 아니면 숫자 관련 설정을 자동으로 해제하는 방어 코드를 추가해 드립니다.
또한, 개별 노드나 허브의 오류가 플랫폼 전체의 로드를 방해하지 않도록 예외 처리를 강화했습니다.
"""

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up asyncua_sensor coordinator_nodes."""

    coordinator_nodes: dict[str, list[dict[str, str]]] = {}
    coordinators: dict[str, AsyncuaCoordinator] = {}
    asyncua_sensors: list = []

    try:
        # 허브별 노드 분류 및 취합
        for _idx_node, val_node in enumerate(config[CONF_NODES]):
            if val_node[CONF_NODE_HUB] not in coordinator_nodes.keys():
                coordinator_nodes[val_node[CONF_NODE_HUB]] = []
            coordinator_nodes[val_node[CONF_NODE_HUB]].append(val_node)

        for key_coordinator, val_coordinator in coordinator_nodes.items():
            # 허브 존재 여부 확인
            if key_coordinator not in hass.data[DOMAIN].keys():
                _LOGGER.error("Asyncua hub %s not found. Check your configuration.", key_coordinator)
                continue # 특정 허브가 없어도 다른 허브는 진행

            coordinators[key_coordinator] = hass.data[DOMAIN][key_coordinator]
            coordinators[key_coordinator].add_sensors(sensors=val_coordinator)

            # 센서 객체 생성
            for _idx_sensor, val_sensor in enumerate(val_coordinator):
                try:
                    asyncua_sensors.append(
                        AsyncuaSensor(
                            coordinator=coordinators[key_coordinator],
                            name=val_sensor[CONF_NODE_NAME],
                            unique_id=val_sensor.get(CONF_NODE_UNIQUE_ID),
                            hub=val_sensor[CONF_NODE_HUB],
                            node_id=val_sensor[CONF_NODE_ID],
                            device_class=val_sensor.get(CONF_NODE_DEVICE_CLASS),
                            unit_of_measurement=val_sensor.get(CONF_NODE_UNIT_OF_MEASUREMENT),
                        )
                    )
                except Exception as sensor_err:
                    _LOGGER.error("Failed to create sensor %s: %s", val_sensor.get(CONF_NODE_NAME), sensor_err)

        # 최종 엔티티 추가
        async_add_entities(new_entities=asyncua_sensors)

    except Exception as critical_err:
        _LOGGER.critical("Critical error during asyncua sensor setup: %s", critical_err)


class AsyncuaSensor(CoordinatorEntity[AsyncuaCoordinator], SensorEntity):
    """A sensor implementation for Asyncua OPCUA nodes."""

    def __init__(
        self,
        coordinator: AsyncuaCoordinator,
        name: str,
        hub: str,
        node_id: str,
        device_class: Any,
        unique_id: Union[str, None] = None,
        state_class: str = "measurement",
        precision: int = 2,
        unit_of_measurement: Union[str, None] = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator=coordinator)
        self._attr_name = name
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{DOMAIN}.{hub}.{node_id}"
        )
        self._attr_available = False
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_native_value = None

        # 🚨 [수정 1] 초기화 시점에 바로 state_class를 할당하지 않고 나중에 결정합니다.
        self._initial_state_class = state_class
        self._initial_device_class = device_class
        self._initial_uom = unit_of_measurement
        self._attr_state_class = state_class
        self._attr_suggested_display_precision = precision

        self._hub = hub
        self._node_id = node_id

        # 초기 데이터 파싱 시도
        try:
            self._attr_native_value = self._parse_coordinator_data(
                coordinator_data=coordinator.data
            )
        except Exception:
            self._attr_native_value = None

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
        """Parse the value from the mapped coordinator with safety."""
        try:
            if self._attr_name is None:
                return None
            return coordinator_data.get(self._attr_name)
        except Exception as err:
            _LOGGER.debug("Parsing error for %s: %s", self._attr_name, err)
            return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle update of the data with string-to-numeric defense logic."""
        try:
            raw_value = self._parse_coordinator_data(
                coordinator_data=self.coordinator.data,
            )

            # 🚨 [수정 2] 방어 로직: 값이 숫자가 아닌 문자열(예: 날짜)인 경우 처리
            if isinstance(raw_value, str):
                try:
                    # 숫자로 변환 시도 (float로 변환 가능하면 숫자 센서로 유지)
                    float(raw_value)
                    self._attr_state_class = self._initial_state_class
                    self._attr_device_class = self._initial_device_class
                    self._attr_native_unit_of_measurement = self._initial_uom
                except (ValueError, TypeError):
                    # 🔴 숫자가 아닌 문자열인 경우, 숫자 관련 속성을 강제로 제거
                    # 이 조치가 sensor/__init__.py:691의 ValueError를 원천 차단합니다.
                    self._attr_state_class = None
                    self._attr_device_class = None
                    self._attr_native_unit_of_measurement = None
                    self._attr_suggested_display_precision = None
            else:
                # 값이 숫자(int/float)라면 원래 설정대로 복구
                self._attr_state_class = self._initial_state_class

            self._attr_native_value = raw_value
            self._attr_available = True if raw_value is not None else False
            self.async_write_ha_state()

        except Exception as handle_err:
            _LOGGER.error("Error in update handler for %s: %s", self._attr_name, handle_err)