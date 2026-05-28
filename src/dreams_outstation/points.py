from __future__ import annotations

from typing import Any

from .models import AiPoint, AoPoint

GROUP30_VAR3 = 3
GROUP30_VAR6 = 6
GROUP32_VAR3 = 3
GROUP32_VAR6 = 6


AI_POINTS: dict[int, AiPoint] = {
    0: AiPoint(0, "Line Current Phase A", "0.1A", 10, GROUP30_VAR3, GROUP32_VAR3, 500),
    1: AiPoint(1, "Line Current Phase B", "0.1A", 10, GROUP30_VAR3, GROUP32_VAR3, 500),
    2: AiPoint(2, "Line Current Phase C", "0.1A", 10, GROUP30_VAR3, GROUP32_VAR3, 500),
    3: AiPoint(3, "Line Current Phase N", "0.1A", 10, GROUP30_VAR3, GROUP32_VAR3, 500),
    4: AiPoint(4, "Line Voltage Phase AB", "0.01V", 100, GROUP30_VAR3, GROUP32_VAR3, 100),
    5: AiPoint(5, "Line Voltage Phase BC", "0.01V", 100, GROUP30_VAR3, GROUP32_VAR3, 100),
    6: AiPoint(6, "Line Voltage Phase AC", "0.01V", 100, GROUP30_VAR3, GROUP32_VAR3, 100),
    7: AiPoint(7, "Active Power", "W", 1, GROUP30_VAR3, GROUP32_VAR3, 200),
    8: AiPoint(8, "Reactive Power", "Var", 1, GROUP30_VAR3, GROUP32_VAR3, 500),
    9: AiPoint(9, "Power Factor", "%", 1, GROUP30_VAR3, GROUP32_VAR3, 100),
    10: AiPoint(10, "Frequency", "0.1Hz", 10, GROUP30_VAR3, GROUP32_VAR3, 50),
    11: AiPoint(11, "Accumulated Energy", "Wh", 1, GROUP30_VAR6, GROUP32_VAR6, None, class2_enabled=False),
    12: AiPoint(12, "Irradiance", "W/m2", 1, GROUP30_VAR3, GROUP32_VAR3, None, class2_enabled=False),
    13: AiPoint(13, "Wind Speed", "m/s", 1, GROUP30_VAR3, GROUP32_VAR3, None, class2_enabled=False),
    14: AiPoint(14, "Inverter PF Setpoint", "%", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    15: AiPoint(15, "Inverter Active Power Setpoint", "%", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    16: AiPoint(16, "Inverter Reactive Power Setpoint", "%", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    17: AiPoint(17, "Inverter Vpset", "Int", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    18: AiPoint(18, "Inverter 1-25 Control Success Bitmask", "25bit", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    19: AiPoint(19, "Inverter 26-50 Control Success Bitmask", "25bit", 1, GROUP30_VAR3, GROUP32_VAR3, None),
    20: AiPoint(20, "Line Current Phase A Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    21: AiPoint(21, "Line Current Phase B Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    22: AiPoint(22, "Line Current Phase C Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    23: AiPoint(23, "Line Current Phase N Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    24: AiPoint(24, "Line Voltage Phase AB Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    25: AiPoint(25, "Line Voltage Phase BC Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    26: AiPoint(26, "Line Voltage Phase AC Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    27: AiPoint(27, "Active Power Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 1.0),
    28: AiPoint(28, "Reactive Power Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 1.0),
    29: AiPoint(29, "Power Factor Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 1.0),
    30: AiPoint(30, "Frequency Dead Band Setting", "0.01%", 100, GROUP30_VAR3, GROUP32_VAR3, None, 0.5),
    31: AiPoint(31, "Spare", "-", 1, GROUP30_VAR3, GROUP32_VAR3, None, 0.0, enabled=False),
    32: AiPoint(32, "Timestamp", "unix time", 1, GROUP30_VAR3, GROUP32_VAR3, None, class2_enabled=False),
}


AO_POINTS: dict[int, AoPoint] = {
    0: AoPoint(0, "Set inverter PF", "%", "control", "pf_percent", 14),
    1: AoPoint(1, "Set inverter active power", "%", "control", "active_power_percent", 15),
    2: AoPoint(2, "Set inverter reactive power", "Var", "control", "reactive_power_percent", 16, reserved=True),
    3: AoPoint(3, "Set inverter Vpset", "Int", "control", "vpset", 17),
    4: AoPoint(4, "Set autonomous control", "-", "control", "autonomous_control", None),
    5: AoPoint(5, "Set AI_0 dead band", "0.01%", "config_deadband", "Deadband_AI_0", 20, 0.01),
    6: AoPoint(6, "Set AI_1 dead band", "0.01%", "config_deadband", "Deadband_AI_1", 21, 0.01),
    7: AoPoint(7, "Set AI_2 dead band", "0.01%", "config_deadband", "Deadband_AI_2", 22, 0.01),
    8: AoPoint(8, "Set AI_3 dead band", "0.01%", "config_deadband", "Deadband_AI_3", 23, 0.01),
    9: AoPoint(9, "Set AI_4 dead band", "0.01%", "config_deadband", "Deadband_AI_4", 24, 0.01),
    10: AoPoint(10, "Set AI_5 dead band", "0.01%", "config_deadband", "Deadband_AI_5", 25, 0.01),
    11: AoPoint(11, "Set AI_6 dead band", "0.01%", "config_deadband", "Deadband_AI_6", 26, 0.01),
    12: AoPoint(12, "Set AI_7 dead band", "0.01%", "config_deadband", "Deadband_AI_7", 27, 0.01),
    13: AoPoint(13, "Set AI_8 dead band", "0.01%", "config_deadband", "Deadband_AI_8", 28, 0.01),
    14: AoPoint(14, "Set AI_9 dead band", "0.01%", "config_deadband", "Deadband_AI_9", 29, 0.01),
    15: AoPoint(15, "Set AI_10 dead band", "0.01%", "config_deadband", "Deadband_AI_10", 30, 0.01),
}


def enabled_ai_points(include_spare_point_31: bool = False) -> dict[int, AiPoint]:
    return {
        index: point
        for index, point in AI_POINTS.items()
        if point.enabled or (index == 31 and include_spare_point_31)
    }


def normalize_ai_key(key: str | int) -> int:
    if isinstance(key, int):
        return key
    value = str(key).strip()
    if value.upper().startswith("AI_"):
        value = value[3:]
    return int(value)


def build_mqtt_command(ao_index: int, raw_value: float | int, cmd_id: str) -> dict[str, Any]:
    if ao_index not in AO_POINTS:
        raise KeyError(f"Unsupported AO index: {ao_index}")

    point = AO_POINTS[ao_index]
    value = point.engineering_value(raw_value)
    payload = {
        "cmd_id": cmd_id,
        "type": point.command_type,
        "target": point.target,
        "value": value,
        "unit": "%" if point.unit == "0.01%" else point.unit,
        "raw_ao_index": ao_index,
        "raw_value": raw_value,
    }
    if point.reserved:
        payload["reserved"] = True
    return payload
