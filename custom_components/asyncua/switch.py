"""Platform for switch integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
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

# 노드 설정을 위한 스키마 정의
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

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up asyncua_switch 플랫폼 설정."""

    coordinator_nodes: dict[str, list[dict[str, str]]] = {}
    asyncua_switches: list = []

    # 허브별로 노드를 분류합니다.
    for val_node in config[CONF_NODES]:
        hub_name = val_node[CONF_NODE_HUB]
        if hub_name not in coordinator_nodes:
            coordinator_nodes[hub_name] = []
        coordinator_nodes[hub_name].append(val_node)

    for key_coordinator, val_coordinator in coordinator_nodes.items():
        # 도메인 내 허브 존재 여부 확인
        if key_coordinator not in hass.data[DOMAIN]:
            _LOGGER.error("Asyncua hub %s not found in hass.data", key_coordinator)
            continue

        coordinator = hass.data[DOMAIN][key_coordinator]
        coordinator.add_sensors(sensors=val_coordinator)

        for val_sensor in val_coordinator:
            asyncua_switches.append(
                AsyncuaSwitch(
                    coordinator=coordinator,
                    name=val_sensor[CONF_NODE_NAME],
                    hub=val_sensor[CONF_NODE_HUB],
                    node_id=val_sensor[CONF_NODE_ID],
                    addr_di=val_sensor.get(CONF_NODE_SWITCH_DI),
                    unique_id=val_sensor.get(CONF_NODE_UNIQUE_ID),
                )
            )

    # 엔티티 일괄 추가 (초기화 블로킹 방지를 위해 async_init은 내부 루프로 처리하지 않음)
    async_add_entities(asyncua_switches)


class AsyncuaSwitch(CoordinatorEntity[AsyncuaCoordinator], SwitchEntity):
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
        """스위치 엔티티 초기화."""
        super().__init__(coordinator=coordinator)
        self._attr_name = name
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{DOMAIN}.{hub}.{node_id}"
        )
        # 🚨 [수정] STATE_UNAVAILABLE 대신 Boolean(False)으로 초기 가용성 설정
        self._attr_available = False
        self._attr_device_class = SwitchDeviceClass.SWITCH

        self._hub = hub
        self._node_id = node_id
        # 피드백 주소(DI)가 없으면 쓰기 주소(node_id)를 그대로 사용
        self._addr_di = addr_di if addr_di is not None else node_id

    @property
    def is_on(self) -> bool | None:
        """코디네이터의 데이터를 기반으로 스위치 On/Off 상태 반환."""
        if self.coordinator.data is None:
            return None

        # 🚨 [중요] 코디네이터에서 '이름' 기반으로 데이터를 추출
        raw_val = self.coordinator.data.get(self._attr_name)

        if raw_val is None:
            self._attr_available = False
            return None

        self._attr_available = True
        # 숫자(0, 1)나 불리언 모두 대응
        return bool(raw_val)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """스위치를 켭니다 (서버로 1 전송)."""
        await self._async_set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """스위치를 끕니다 (서버로 0 전송)."""
        await self._async_set_value(False)

    async def _async_set_value(self, val: bool) -> None:
        """서버에 값을 기록하고 상태를 즉시 업데이트합니다."""
        try:
            # 🚨 [핵심] 아르헨티나 서버 규격(Int64)에 맞춰 0/1로 변환 전송
            target_val = 1 if val else 0

            # 허브를 통해 서버에 값 기록
            await self.coordinator.hub.set_value(
                nodeid=self._node_id,
                value=target_val
            )

            # 🚨 [개선] 낙관적 업데이트: 서버 응답 전 UI 상태를 먼저 변경하여 반응성 향상
            self._attr_is_on = val
            self.async_write_ha_state()

            # 제어 후 다른 센서들의 상태도 갱신하도록 요청
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Error setting switch %s value: %s", self._attr_name, err)
            self._attr_available = False
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """코디네이터 데이터가 변경되면 호출됩니다."""
        # is_on 프로퍼티가 가용성 및 상태를 판단하므로 상태 기록만 수행
        self.async_write_ha_state()