#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 + RS485 3포트 확장 보드
초음파 일체형 종합기상센서 데이터 수집 프로그램

제품 프로토콜
- Modbus RTU
- 기본 Slave ID: 1
- 기본 Baudrate: 4800
- 8N1
- 기본 Function Code: 03
- Register: 500 ~ 515
"""

from __future__ import annotations

import csv
import json
import logging
import signal
import sys
import time

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException


# ============================================================
# 사용자 설정
# ============================================================

# 전체 센서 수집 주기
POLL_INTERVAL_SEC = 2.0

# 센서 요청 사이 최소 간격
# 매뉴얼에서 200ms 이상 권장
REQUEST_GAP_SEC = 0.25

# Modbus 응답 대기시간
MODBUS_TIMEOUT_SEC = 1.0

# CSV 및 JSON 저장 경로
CSV_PATH = Path("weather_sensor_data.csv")
LATEST_JSON_PATH = Path("latest_weather_data.json")

# 레지스터 설정
REGISTER_START = 500
REGISTER_COUNT = 16


@dataclass(frozen=True)
class SensorConfig:
    """
    RS485 포트별 센서 설정.

    register_507_type:
        "co2"   : 507번 레지스터를 CO2로 사용
        "pm2_5" : 507번 레지스터를 PM2.5로 사용
    """

    name: str
    port: str

    slave_id: int = 1
    baudrate: int = 4800

    # 3: Holding Register
    # 4: Input Register
    function_code: int = 3

    register_507_type: str = "co2"


# ------------------------------------------------------------
# 실제 확장보드의 장치명에 맞게 수정
# ------------------------------------------------------------

SENSORS = [
    SensorConfig(
        name="외부센서",
        port="/dev/ttyAMA0",
        slave_id=1,
        baudrate=4800,
        function_code=3,
        register_507_type="co2",
    ),
    SensorConfig(
        name="내부센서1",
        port="/dev/ttyAMA2",
        slave_id=1,
        baudrate=4800,
        function_code=3,
        register_507_type="co2",
    ),
    SensorConfig(
        name="내부센서2",
        port="/dev/ttyAMA3",
        slave_id=1,
        baudrate=4800,
        function_code=3,
        register_507_type="co2",
    ),
]


# ============================================================
# 로그 설정
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | %(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("weather485")


# ============================================================
# 데이터 구조
# ============================================================

CSV_FIELDS = [
    "poll_time",
    "data_time",
    "sensor_name",
    "port",
    "slave_id",
    "status",
    "error",
    "stale_age_sec",

    "wind_speed_ms",
    "wind_force",
    "wind_direction_sector",
    "wind_direction_deg",

    "humidity_pct",
    "temperature_c",

    "noise_db",
    "co2_ppm",
    "pm2_5_ugm3",
    "pm10_ugm3",

    "pressure_kpa",

    "illuminance_lux",
    "illuminance_coarse_lux",

    "rainfall_mm",
    "compass_deg",
    "solar_radiation_wm2",
]


def now_iso() -> str:
    """현재 시각을 ISO 8601 형식으로 반환합니다."""

    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def uint16_to_int16(value: int) -> int:
    """
    Modbus unsigned 16-bit 값을 signed 16-bit로 변환합니다.

    예:
        0xFF9B = 65435
        signed 변환 후 -101
        온도 환산 후 -10.1℃
    """

    value &= 0xFFFF

    if value >= 0x8000:
        return value - 0x10000

    return value


def decode_registers(
    registers: list[int],
    register_507_type: str,
) -> dict[str, Any]:
    """
    레지스터 500~515를 실제 측정값으로 변환합니다.
    """

    if len(registers) != REGISTER_COUNT:
        raise ValueError(
            f"레지스터 개수가 올바르지 않습니다: "
            f"{len(registers)}개"
        )

    # --------------------------------------------------------
    # 레지스터 매핑
    # registers[0]  = 500
    # registers[15] = 515
    # --------------------------------------------------------

    wind_speed_raw = registers[0]
    wind_force_raw = registers[1]
    wind_direction_sector_raw = registers[2]
    wind_direction_degree_raw = registers[3]

    humidity_raw = registers[4]
    temperature_raw = uint16_to_int16(registers[5])

    noise_raw = registers[6]
    register_507_raw = registers[7]
    pm10_raw = registers[8]

    pressure_raw = registers[9]

    lux_high = registers[10]
    lux_low = registers[11]

    lux_coarse_raw = registers[12]
    rainfall_raw = registers[13]
    compass_raw = registers[14]
    solar_radiation_raw = registers[15]

    # 510번을 상위 16비트, 511번을 하위 16비트로 결합
    illuminance_32bit = (
        (lux_high << 16) | lux_low
    )

    co2_ppm: Optional[int] = None
    pm2_5_ugm3: Optional[int] = None

    if register_507_type.lower() == "co2":
        co2_ppm = register_507_raw

    elif register_507_type.lower() == "pm2_5":
        pm2_5_ugm3 = register_507_raw

    else:
        raise ValueError(
            "register_507_type은 "
            "'co2' 또는 'pm2_5'이어야 합니다."
        )

    decoded = {
        "wind_speed_ms": wind_speed_raw / 10.0,
        "wind_force": wind_force_raw,
        "wind_direction_sector": (
            wind_direction_sector_raw
        ),
        "wind_direction_deg": (
            wind_direction_degree_raw
        ),

        "humidity_pct": humidity_raw / 10.0,
        "temperature_c": temperature_raw / 10.0,

        "noise_db": noise_raw / 10.0,

        "co2_ppm": co2_ppm,
        "pm2_5_ugm3": pm2_5_ugm3,
        "pm10_ugm3": pm10_raw,

        "pressure_kpa": pressure_raw / 10.0,

        # 정밀 조도값
        "illuminance_lux": illuminance_32bit,

        # 512번 레지스터: 100 lux 단위
        "illuminance_coarse_lux": (
            lux_coarse_raw * 100
        ),

        "rainfall_mm": rainfall_raw / 10.0,
        "compass_deg": compass_raw / 100.0,

        "solar_radiation_wm2": (
            solar_radiation_raw
        ),

        # 통신 확인용 원시 레지스터
        "raw_registers": registers.copy(),
    }

    return decoded


def validate_values(
    data: dict[str, Any],
) -> list[str]:
    """
    프로토콜에 명시된 범위를 기준으로
    명백히 비정상적인 값을 확인합니다.

    경고만 생성하며 데이터 자체는 제거하지 않습니다.
    """

    warnings: list[str] = []

    temperature = data.get("temperature_c")
    humidity = data.get("humidity_pct")
    co2 = data.get("co2_ppm")
    wind_speed = data.get("wind_speed_ms")
    wind_direction = data.get("wind_direction_deg")
    pressure = data.get("pressure_kpa")
    solar = data.get("solar_radiation_wm2")

    if temperature is not None:
        if not -40.0 <= temperature <= 80.0:
            warnings.append(
                f"온도 범위 이상: {temperature}℃"
            )

    if humidity is not None:
        if not 0.0 <= humidity <= 99.0:
            warnings.append(
                f"습도 범위 이상: {humidity}%"
            )

    if co2 is not None:
        if not 0 <= co2 <= 5000:
            warnings.append(
                f"CO2 범위 이상: {co2}ppm"
            )

    if wind_speed is not None:
        if not 0.0 <= wind_speed <= 40.0:
            warnings.append(
                f"풍속 범위 이상: {wind_speed}m/s"
            )

    if wind_direction is not None:
        if not 0 <= wind_direction <= 360:
            warnings.append(
                f"풍향 범위 이상: {wind_direction}°"
            )

    if pressure is not None:
        if not 0.0 <= pressure <= 120.0:
            warnings.append(
                f"기압 범위 이상: {pressure}kPa"
            )

    if solar is not None:
        if not 0 <= solar <= 1800:
            warnings.append(
                f"일사량 범위 이상: {solar}W/㎡"
            )

    return warnings


# ============================================================
# 센서 통신 클래스
# ============================================================

class WeatherSensorNode:
    """RS485 포트 하나에 연결된 종합센서입니다."""

    def __init__(
        self,
        config: SensorConfig,
    ) -> None:

        self.config = config

        self.client = ModbusSerialClient(
            port=config.port,
            baudrate=config.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=MODBUS_TIMEOUT_SEC,
            retries=2,
        )

        self.is_connected = False

        self.last_good_data: Optional[
            dict[str, Any]
        ] = None

        self.last_good_monotonic: Optional[
            float
        ] = None

    def connect(self) -> bool:
        """시리얼 포트 연결을 시도합니다."""

        try:
            self.is_connected = bool(
                self.client.connect()
            )

        except Exception as error:
            self.is_connected = False

            logger.error(
                "%s 포트 연결 오류: %s",
                self.config.name,
                error,
            )

        if self.is_connected:
            logger.info(
                "%s 연결 성공: %s, ID=%d, %dbps",
                self.config.name,
                self.config.port,
                self.config.slave_id,
                self.config.baudrate,
            )

        else:
            logger.error(
                "%s 연결 실패: %s",
                self.config.name,
                self.config.port,
            )

        return self.is_connected

    def disconnect(self) -> None:
        """시리얼 포트를 닫습니다."""

        try:
            self.client.close()
        except Exception:
            pass

        self.is_connected = False

    def _read_registers(self) -> list[int]:
        """FC03 또는 FC04로 레지스터를 읽습니다."""

        if self.config.function_code == 3:
            response = (
                self.client.read_holding_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    device_id=self.config.slave_id,
                )
            )

        elif self.config.function_code == 4:
            response = (
                self.client.read_input_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    device_id=self.config.slave_id,
                )
            )

        else:
            raise ValueError(
                "function_code는 3 또는 4여야 합니다."
            )

        if response.isError():
            raise RuntimeError(
                f"Modbus 예외 응답: {response}"
            )

        if not hasattr(response, "registers"):
            raise RuntimeError(
                "응답에 registers 데이터가 없습니다."
            )

        registers = list(response.registers)

        if len(registers) != REGISTER_COUNT:
            raise RuntimeError(
                f"응답 레지스터 부족: "
                f"{len(registers)}/{REGISTER_COUNT}"
            )

        return registers

    def read(
        self,
    ) -> tuple[
        Optional[dict[str, Any]],
        str,
        str,
        Optional[float],
    ]:
        """
        센서를 읽습니다.

        Returns:
            data
            status
            error
            stale_age_sec
        """

        if not self.is_connected:
            if not self.connect():
                return self._stale_result(
                    "PORT_CONNECT_FAILED"
                )

        try:
            registers = self._read_registers()

            decoded = decode_registers(
                registers=registers,
                register_507_type=(
                    self.config.register_507_type
                ),
            )

            decoded["data_time"] = now_iso()

            warnings = validate_values(decoded)

            if warnings:
                decoded["validation_warning"] = (
                    "; ".join(warnings)
                )
            else:
                decoded["validation_warning"] = ""

            self.last_good_data = decoded
            self.last_good_monotonic = (
                time.monotonic()
            )

            return decoded, "OK", "", 0.0

        except (
            ModbusException,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:

            logger.warning(
                "%s 통신 오류: %s",
                self.config.name,
                error,
            )

            self.disconnect()

            return self._stale_result(str(error))

        except Exception as error:
            logger.exception(
                "%s 예상하지 못한 오류",
                self.config.name,
            )

            self.disconnect()

            return self._stale_result(str(error))

    def _stale_result(
        self,
        error: str,
    ) -> tuple[
        Optional[dict[str, Any]],
        str,
        str,
        Optional[float],
    ]:
        """오류 시 마지막 정상값을 반환합니다."""

        if self.last_good_data is None:
            return None, "ERROR", error, None

        if self.last_good_monotonic is None:
            stale_age = None
        else:
            stale_age = (
                time.monotonic()
                - self.last_good_monotonic
            )

        return (
            self.last_good_data.copy(),
            "STALE",
            error,
            stale_age,
        )


# ============================================================
# 파일 저장
# ============================================================

def initialize_csv() -> None:
    """CSV 파일이 없으면 헤더를 생성합니다."""

    if CSV_PATH.exists():
        return

    with CSV_PATH.open(
        mode="w",
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:

        writer = csv.DictWriter(
            csv_file,
            fieldnames=CSV_FIELDS,
        )

        writer.writeheader()


def append_csv(
    poll_time: str,
    node: WeatherSensorNode,
    data: Optional[dict[str, Any]],
    status: str,
    error: str,
    stale_age_sec: Optional[float],
) -> None:
    """측정 결과를 CSV에 저장합니다."""

    row: dict[str, Any] = {
        field: ""
        for field in CSV_FIELDS
    }

    row.update(
        {
            "poll_time": poll_time,
            "sensor_name": node.config.name,
            "port": node.config.port,
            "slave_id": node.config.slave_id,
            "status": status,
            "error": error,
            "stale_age_sec": (
                ""
                if stale_age_sec is None
                else round(stale_age_sec, 1)
            ),
        }
    )

    if data is not None:
        for field in CSV_FIELDS:
            if field in data:
                row[field] = data[field]

    with CSV_PATH.open(
        mode="a",
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:

        writer = csv.DictWriter(
            csv_file,
            fieldnames=CSV_FIELDS,
        )

        writer.writerow(row)


def save_latest_json(
    latest_data: dict[str, Any],
) -> None:
    """
    최신 센서값을 JSON으로 저장합니다.

    임시 파일에 저장한 후 교체하여,
    웹 프로그램이 읽는 중 파일이 깨지는 것을 방지합니다.
    """

    temporary_path = (
        LATEST_JSON_PATH.with_suffix(".tmp")
    )

    with temporary_path.open(
        mode="w",
        encoding="utf-8",
    ) as json_file:

        json.dump(
            latest_data,
            json_file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_path.replace(LATEST_JSON_PATH)


# ============================================================
# 화면 출력
# ============================================================

def format_value(
    value: Any,
    digits: int = 1,
) -> str:
    """None 값과 숫자를 화면 출력용 문자열로 변환합니다."""

    if value is None:
        return "--"

    if isinstance(value, float):
        return f"{value:.{digits}f}"

    return str(value)


def print_sensor_data(
    node: WeatherSensorNode,
    data: Optional[dict[str, Any]],
    status: str,
    error: str,
    stale_age_sec: Optional[float],
) -> None:
    """주요 센서값을 터미널에 출력합니다."""

    if data is None:
        logger.error(
            "%s | 데이터 없음 | %s",
            node.config.name,
            error,
        )
        return

    stale_text = ""

    if stale_age_sec is not None and status == "STALE":
        stale_text = (
            f" | 이전값 {stale_age_sec:.1f}초 경과"
        )

    logger.info(
        (
            "%s | %s | "
            "온도=%s℃ | "
            "습도=%s%% | "
            "CO2=%sppm | "
            "조도=%slux | "
            "일사=%sW/㎡ | "
            "풍속=%sm/s | "
            "강우=%smm%s"
        ),
        node.config.name,
        status,
        format_value(data.get("temperature_c")),
        format_value(data.get("humidity_pct")),
        format_value(data.get("co2_ppm"), 0),
        format_value(
            data.get("illuminance_lux"),
            0,
        ),
        format_value(
            data.get("solar_radiation_wm2"),
            0,
        ),
        format_value(data.get("wind_speed_ms")),
        format_value(data.get("rainfall_mm")),
        stale_text,
    )


# ============================================================
# 메인 루프
# ============================================================

running = True


def stop_handler(
    signum: int,
    frame: Any,
) -> None:
    """Ctrl+C 또는 종료 신호 처리."""

    del signum
    del frame

    global running
    running = False


def main() -> int:
    """프로그램 시작점입니다."""

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    initialize_csv()

    nodes = [
        WeatherSensorNode(config)
        for config in SENSORS
    ]

    logger.info("=" * 70)
    logger.info(
        "라즈베리파이5 RS485 종합기상센서 수집 시작"
    )
    logger.info(
        "레지스터: %d~%d",
        REGISTER_START,
        REGISTER_START + REGISTER_COUNT - 1,
    )
    logger.info(
        "수집 주기: %.1f초",
        POLL_INTERVAL_SEC,
    )
    logger.info(
        "CSV 저장: %s",
        CSV_PATH.resolve(),
    )
    logger.info(
        "JSON 저장: %s",
        LATEST_JSON_PATH.resolve(),
    )
    logger.info("=" * 70)

    try:
        while running:
            cycle_start = time.monotonic()
            poll_time = now_iso()

            latest_result: dict[str, Any] = {
                "updated_at": poll_time,
                "sensors": {},
            }

            for index, node in enumerate(nodes):
                (
                    data,
                    status,
                    error,
                    stale_age_sec,
                ) = node.read()

                print_sensor_data(
                    node=node,
                    data=data,
                    status=status,
                    error=error,
                    stale_age_sec=stale_age_sec,
                )

                append_csv(
                    poll_time=poll_time,
                    node=node,
                    data=data,
                    status=status,
                    error=error,
                    stale_age_sec=stale_age_sec,
                )

                latest_result["sensors"][
                    node.config.name
                ] = {
                    "port": node.config.port,
                    "slave_id": (
                        node.config.slave_id
                    ),
                    "status": status,
                    "error": error,
                    "stale_age_sec": stale_age_sec,
                    "data": data,
                }

                # 요청 간격 확보
                if index < len(nodes) - 1:
                    time.sleep(REQUEST_GAP_SEC)

            save_latest_json(latest_result)

            elapsed = (
                time.monotonic() - cycle_start
            )

            sleep_time = (
                POLL_INTERVAL_SEC - elapsed
            )

            if sleep_time > 0:
                time.sleep(sleep_time)

    finally:
        for node in nodes:
            node.disconnect()

        logger.info(
            "종합기상센서 수집 프로그램 종료"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
