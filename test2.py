#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 + RS485 병렬 3단자 종합센서 테스트

구조
- GPIO14: UART TX
- GPIO15: UART RX
- Linux UART: /dev/ttyAMA0
- UART/RS485 버스 1개
- 종합센서 3대 병렬 연결
- 센서별 Slave ID로 구분

센서 설정
- 외부센서   : Slave ID 1
- 내부센서 1: Slave ID 2
- 내부센서 2: Slave ID 3

통신 프로토콜
- Modbus RTU
- Baudrate: 4800
- 8N1
- Function Code: 03
- Register: 500~515
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pymodbus.client import ModbusSerialClient


# ============================================================
# 통신 설정
# ============================================================

SERIAL_PORT = "/dev/ttyAMA0"

BAUDRATE = 4800
BYTESIZE = 8
PARITY = "N"
STOPBITS = 1

SLAVE_TIMEOUT_SEC = 1.5

# 센서 요청 사이 간격
# 매뉴얼 권장값 200ms 이상
REQUEST_GAP_SEC = 0.3

# 전체 센서 반복 측정 주기
POLL_INTERVAL_SEC = 5.0

# 종합센서 레지스터
REGISTER_START = 500
REGISTER_COUNT = 16


# ============================================================
# 센서 설정
# ============================================================

SENSORS = [
    {
        "name": "외부센서",
        "slave_id": 1,
        "register_507_type": "co2",
    },
    {
        "name": "내부센서1",
        "slave_id": 2,
        "register_507_type": "co2",
    },
    {
        "name": "내부센서2",
        "slave_id": 3,
        "register_507_type": "co2",
    },
]


# ============================================================
# 값 변환 함수
# ============================================================

def uint16_to_int16(value: int) -> int:
    """
    unsigned 16-bit 값을 signed 16-bit로 변환합니다.

    영하 온도의 경우 센서가 2의 보수로 전송합니다.
    예: 0xFF9B → -101 → -10.1℃
    """

    value &= 0xFFFF

    if value >= 0x8000:
        return value - 0x10000

    return value


def decode_registers(
    registers: list[int],
    register_507_type: str = "co2",
) -> dict[str, Any]:
    """
    레지스터 500~515 값을 실제 측정값으로 변환합니다.
    """

    if len(registers) != REGISTER_COUNT:
        raise ValueError(
            f"레지스터 개수 오류: "
            f"{len(registers)}/{REGISTER_COUNT}"
        )

    # 500: 풍속 × 10
    wind_speed_ms = registers[0] / 10.0

    # 501: 풍력 등급
    wind_force = registers[1]

    # 502: 풍향 0~7단계
    wind_direction_sector = registers[2]

    # 503: 풍향 0~360°
    wind_direction_deg = registers[3]

    # 504: 습도 × 10
    humidity_pct = registers[4] / 10.0

    # 505: 온도 × 10, 음수는 2의 보수
    temperature_raw = uint16_to_int16(registers[5])
    temperature_c = temperature_raw / 10.0

    # 506: 소음 × 10
    noise_db = registers[6] / 10.0

    # 507: 장비 옵션에 따라 CO2 또는 PM2.5
    register_507_value = registers[7]

    co2_ppm = None
    pm2_5_ugm3 = None

    if register_507_type == "co2":
        co2_ppm = register_507_value

    elif register_507_type == "pm2_5":
        pm2_5_ugm3 = register_507_value

    else:
        raise ValueError(
            "register_507_type은 "
            "'co2' 또는 'pm2_5'여야 합니다."
        )

    # 508: PM10
    pm10_ugm3 = registers[8]

    # 509: 대기압 × 10
    pressure_kpa = registers[9] / 10.0

    # 510, 511: 조도 상위/하위 16비트
    lux_high = registers[10]
    lux_low = registers[11]

    illuminance_lux = (
        (lux_high << 16) | lux_low
    )

    # 512: 100 Lux 단위 조도
    illuminance_coarse_lux = registers[12] * 100

    # 513: 강우량 × 10
    rainfall_mm = registers[13] / 10.0

    # 514: 전자나침반 각도 × 100
    compass_deg = registers[14] / 100.0

    # 515: 일사량
    solar_radiation_wm2 = registers[15]

    return {
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "co2_ppm": co2_ppm,

        "illuminance_lux": illuminance_lux,
        "illuminance_coarse_lux": illuminance_coarse_lux,
        "solar_radiation_wm2": solar_radiation_wm2,

        "wind_speed_ms": wind_speed_ms,
        "wind_force": wind_force,
        "wind_direction_sector": wind_direction_sector,
        "wind_direction_deg": wind_direction_deg,

        "noise_db": noise_db,
        "pm2_5_ugm3": pm2_5_ugm3,
        "pm10_ugm3": pm10_ugm3,

        "pressure_kpa": pressure_kpa,
        "rainfall_mm": rainfall_mm,
        "compass_deg": compass_deg,

        # 통신 점검용 원시 레지스터
        "raw_registers": registers,
    }


# ============================================================
# Modbus 읽기
# ============================================================

def read_holding_registers(
    client: ModbusSerialClient,
    slave_id: int,
):
    """
    PyModbus 버전에 따라 device_id 또는 slave 인자를 사용합니다.
    """

    try:
        return client.read_holding_registers(
            address=REGISTER_START,
            count=REGISTER_COUNT,
            device_id=slave_id,
        )

    except TypeError:
        # 일부 이전 PyModbus 버전 대응
        return client.read_holding_registers(
            address=REGISTER_START,
            count=REGISTER_COUNT,
            slave=slave_id,
        )


def read_sensor(
    client: ModbusSerialClient,
    sensor: dict[str, Any],
) -> dict[str, Any] | None:
    """
    지정한 Slave ID의 센서값을 읽습니다.
    """

    sensor_name = sensor["name"]
    slave_id = sensor["slave_id"]

    try:
        response = read_holding_registers(
            client=client,
            slave_id=slave_id,
        )

        if response is None:
            print(
                f"[통신 실패] {sensor_name} "
                f"ID={slave_id}: 응답 없음"
            )
            return None

        if response.isError():
            print(
                f"[Modbus 오류] {sensor_name} "
                f"ID={slave_id}: {response}"
            )
            return None

        if not hasattr(response, "registers"):
            print(
                f"[응답 오류] {sensor_name} "
                f"ID={slave_id}: 레지스터 없음"
            )
            return None

        registers = list(response.registers)

        data = decode_registers(
            registers=registers,
            register_507_type=sensor["register_507_type"],
        )

        data["sensor_name"] = sensor_name
        data["slave_id"] = slave_id
        data["timestamp"] = (
            datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        )

        return data

    except Exception as error:
        print(
            f"[읽기 예외] {sensor_name} "
            f"ID={slave_id}: {error}"
        )
        return None


# ============================================================
# 화면 출력
# ============================================================

def print_sensor_data(data: dict[str, Any]) -> None:
    """측정된 센서값을 터미널에 출력합니다."""

    print()
    print("=" * 65)
    print(
        f"{data['sensor_name']} "
        f"(Slave ID {data['slave_id']})"
    )
    print(f"수신시간       : {data['timestamp']}")
    print("-" * 65)

    print(
        f"온도           : "
        f"{data['temperature_c']:.1f} ℃"
    )

    print(
        f"습도           : "
        f"{data['humidity_pct']:.1f} %RH"
    )

    if data["co2_ppm"] is not None:
        print(
            f"CO2            : "
            f"{data['co2_ppm']} ppm"
        )

    if data["pm2_5_ugm3"] is not None:
        print(
            f"PM2.5          : "
            f"{data['pm2_5_ugm3']} µg/m³"
        )

    print(
        f"조도           : "
        f"{data['illuminance_lux']} lux"
    )

    print(
        f"일사량         : "
        f"{data['solar_radiation_wm2']} W/m²"
    )

    print(
        f"풍속           : "
        f"{data['wind_speed_ms']:.1f} m/s"
    )

    print(
        f"풍향           : "
        f"{data['wind_direction_deg']}°"
    )

    print(
        f"풍력 등급      : "
        f"{data['wind_force']}"
    )

    print(
        f"대기압         : "
        f"{data['pressure_kpa']:.1f} kPa"
    )

    print(
        f"소음           : "
        f"{data['noise_db']:.1f} dB"
    )

    print(
        f"PM10           : "
        f"{data['pm10_ugm3']} µg/m³"
    )

    print(
        f"강우량         : "
        f"{data['rainfall_mm']:.1f} mm"
    )

    print(
        f"전자나침반     : "
        f"{data['compass_deg']:.2f}°"
    )

    print(
        f"원시 레지스터  : "
        f"{data['raw_registers']}"
    )


# ============================================================
# 전체 센서 수집
# ============================================================

def collect_all_sensors(
    client: ModbusSerialClient,
) -> dict[str, dict[str, Any]]:
    """
    동일한 RS485 버스의 센서 3대를 순차적으로 읽습니다.

    반환되는 딕셔너리는 추후 MQTT payload로 바로
    활용할 수 있습니다.
    """

    collected_data: dict[str, dict[str, Any]] = {}

    for index, sensor in enumerate(SENSORS):
        data = read_sensor(
            client=client,
            sensor=sensor,
        )

        if data is not None:
            collected_data[sensor["name"]] = data
            print_sensor_data(data)

        if index < len(SENSORS) - 1:
            time.sleep(REQUEST_GAP_SEC)

    return collected_data


# ============================================================
# 메인 프로그램
# ============================================================

def main() -> None:
    """프로그램 시작 함수."""

    client = ModbusSerialClient(
        port=SERIAL_PORT,
        baudrate=BAUDRATE,
        bytesize=BYTESIZE,
        parity=PARITY,
        stopbits=STOPBITS,
        timeout=SLAVE_TIMEOUT_SEC,
        retries=1,
    )

    print("=" * 65)
    print("RS485 종합센서 3대 통신 테스트")
    print(f"UART 장치       : {SERIAL_PORT}")
    print(f"Baudrate        : {BAUDRATE}")
    print("통신 방식       : Modbus RTU, 8N1")
    print("센서 구성       : ID 1, ID 2, ID 3")
    print("종료            : Ctrl+C")
    print("=" * 65)

    if not client.connect():
        print(
            f"[오류] UART 포트를 열 수 없습니다: "
            f"{SERIAL_PORT}"
        )
        return

    print(
        f"[성공] UART 포트 연결: {SERIAL_PORT}"
    )

    try:
        while True:
            cycle_start = time.monotonic()

            print()
            print("#" * 65)
            print(
                "센서 데이터 수집 시작: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print("#" * 65)

            # 반환값은 추후 MQTT 전송 함수에 전달 가능
            sensor_data = collect_all_sensors(client)

            print()
            print(
                f"정상 수신 센서: "
                f"{len(sensor_data)}/{len(SENSORS)}대"
            )

            elapsed = time.monotonic() - cycle_start
            sleep_time = POLL_INTERVAL_SEC - elapsed

            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print()
        print("사용자 요청으로 프로그램을 종료합니다.")

    finally:
        client.close()
        print("UART 포트를 닫았습니다.")


if __name__ == "__main__":
    main()
