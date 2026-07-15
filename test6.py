#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 + RS485 종합기상센서 1대 테스트

하드웨어
- GPIO14 / 물리 핀 8  : UART TXD0
- GPIO15 / 물리 핀 10 : UART RXD0
- /dev/serial0 -> /dev/ttyAMA0
- RS485 자동 송수신 전환 회로
- 센서 Slave ID: 1

통신
- Modbus RTU
- 4800 bps
- 8N1
- Function Code 03
- Register 500~515

저장 기능 없음
"""

from __future__ import annotations

import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import serial


# ============================================================
# 통신 설정
# ============================================================

SERIAL_PORT = "/dev/serial0"

BAUDRATE = 4800
SLAVE_ID = 1

FIRST_REGISTER = 500
REGISTER_COUNT = 16

SERIAL_TIMEOUT_SEC = 1.5
POLL_INTERVAL_SEC = 2.0
MAX_RETRIES = 3

# True로 변경하면 송수신 프레임과 원시 레지스터 출력
DEBUG_MODE = False


# ============================================================
# 풍향 변환
# ============================================================

WIND_DIRECTION_TEXT = {
    0: "북",
    1: "북동",
    2: "동",
    3: "남동",
    4: "남",
    5: "남서",
    6: "서",
    7: "북서",
}


# ============================================================
# 데이터 구조
# ============================================================

@dataclass(frozen=True)
class WeatherReading:
    """종합기상센서 측정값."""

    timestamp: str
    slave_id: int

    wind_speed_mps: float
    wind_force: int
    wind_direction_octant: int
    wind_direction_text: str
    wind_direction_deg: int

    humidity_pct: float
    temperature_c: float

    noise_db: float
    co2_ppm: int

    pressure_kpa: float

    illuminance_lux: int
    illuminance_coarse_lux: int

    rainfall_mm: float
    compass_deg: float
    solar_radiation_wm2: int

    # 현재 장비에서 CO2와 동일한 값이 확인되므로
    # PM10으로 사용하지 않고 원시값으로만 유지
    register_508_raw: int

    def to_mqtt_payload(self) -> dict[str, Any]:
        """
        추후 MQTT JSON 메시지로 사용할 딕셔너리를 반환합니다.

        register_508_raw는 현재 유효 센서값으로 확인되지 않았으므로
        서버 전송 데이터에서는 제외합니다.
        """

        payload = asdict(self)
        payload.pop("register_508_raw", None)

        return payload


# ============================================================
# Modbus CRC
# ============================================================

def crc16_modbus(data: bytes) -> int:
    """Modbus RTU CRC-16을 계산합니다."""

    crc = 0xFFFF

    for byte in data:
        crc ^= byte

        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1

    return crc & 0xFFFF


def verify_crc_calculation() -> None:
    """
    매뉴얼의 풍속 읽기 요청 예제로 CRC 계산을 확인합니다.

    요청:
        01 03 01 F4 00 01 C4 04
    """

    request_body = bytes(
        [
            0x01,
            0x03,
            0x01,
            0xF4,
            0x00,
            0x01,
        ]
    )

    calculated_crc = crc16_modbus(request_body)

    if calculated_crc != 0x04C4:
        raise RuntimeError(
            "CRC 자체 검사 실패: "
            f"계산값=0x{calculated_crc:04X}, "
            "예상값=0x04C4"
        )


# 프로그램 시작 시 CRC 알고리즘 검사
verify_crc_calculation()


# ============================================================
# 값 변환
# ============================================================

def uint16_to_int16(value: int) -> int:
    """
    unsigned 16-bit를 signed 16-bit로 변환합니다.

    영하 온도는 2의 보수 형식으로 전송됩니다.
    """

    value &= 0xFFFF

    if value >= 0x8000:
        return value - 0x10000

    return value


def decode_registers(
    registers: list[int],
) -> WeatherReading:
    """
    레지스터 500~515를 실제 측정값으로 변환합니다.
    """

    if len(registers) != REGISTER_COUNT:
        raise ValueError(
            f"레지스터 개수 오류: "
            f"{len(registers)}/{REGISTER_COUNT}"
        )

    # 500
    wind_speed_mps = registers[0] / 10.0

    # 501
    wind_force = registers[1]

    # 502
    wind_direction_octant = registers[2]

    wind_direction_text = WIND_DIRECTION_TEXT.get(
        wind_direction_octant,
        "알 수 없음",
    )

    # 503
    wind_direction_deg = registers[3]

    # 504
    humidity_pct = registers[4] / 10.0

    # 505
    temperature_raw = uint16_to_int16(registers[5])
    temperature_c = temperature_raw / 10.0

    # 506
    noise_db = registers[6] / 10.0

    # 507: CO2 타입 장비
    co2_ppm = registers[7]

    # 508: 현재 장비에서는 CO2와 같은 값이 들어오므로
    # 유효 PM10 값으로 판단하지 않고 원시값으로만 보관
    register_508_raw = registers[8]

    # 509
    pressure_kpa = registers[9] / 10.0

    # 510, 511: 조도 상위·하위 16비트 결합
    lux_high = registers[10]
    lux_low = registers[11]

    illuminance_lux = (
        (lux_high << 16)
        | lux_low
    )

    # 512: 100 lux 단위
    illuminance_coarse_lux = registers[12] * 100

    # 513
    rainfall_mm = registers[13] / 10.0

    # 514
    compass_deg = registers[14] / 100.0

    # 515
    solar_radiation_wm2 = registers[15]

    return WeatherReading(
        timestamp=(
            datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        ),
        slave_id=SLAVE_ID,

        wind_speed_mps=wind_speed_mps,
        wind_force=wind_force,
        wind_direction_octant=wind_direction_octant,
        wind_direction_text=wind_direction_text,
        wind_direction_deg=wind_direction_deg,

        humidity_pct=humidity_pct,
        temperature_c=temperature_c,

        noise_db=noise_db,
        co2_ppm=co2_ppm,

        pressure_kpa=pressure_kpa,

        illuminance_lux=illuminance_lux,
        illuminance_coarse_lux=(
            illuminance_coarse_lux
        ),

        rainfall_mm=rainfall_mm,
        compass_deg=compass_deg,
        solar_radiation_wm2=solar_radiation_wm2,

        register_508_raw=register_508_raw,
    )


# ============================================================
# 센서 통신 클래스
# ============================================================

class WeatherStationSensor:
    """종합기상센서 Modbus RTU 통신 클래스."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        slave_id: int,
        timeout: float,
    ) -> None:

        self.slave_id = slave_id
        self.timeout = timeout

        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def close(self) -> None:
        """UART 장치를 닫습니다."""

        if self.serial.is_open:
            self.serial.close()

    def __enter__(self) -> "WeatherStationSensor":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:

        del exc_type
        del exc_value
        del traceback

        self.close()

    def build_read_request(
        self,
        start_address: int,
        quantity: int,
    ) -> bytes:
        """FC03 Holding Register 읽기 요청을 생성합니다."""

        function_code = 0x03

        body = struct.pack(
            ">BBHH",
            self.slave_id,
            function_code,
            start_address,
            quantity,
        )

        crc = crc16_modbus(body)

        # CRC 전송 순서는 Low byte → High byte
        return body + struct.pack("<H", crc)

    def read_exactly(
        self,
        size: int,
    ) -> bytes:
        """
        지정된 크기만큼 수신하거나 timeout까지 기다립니다.
        """

        received = bytearray()
        deadline = time.monotonic() + self.timeout

        while (
            len(received) < size
            and time.monotonic() < deadline
        ):
            remaining = size - len(received)
            chunk = self.serial.read(remaining)

            if chunk:
                received.extend(chunk)

        return bytes(received)

    def read_registers_once(
        self,
        start_address: int,
        quantity: int,
    ) -> list[int]:
        """레지스터 읽기를 한 번 수행합니다."""

        request = self.build_read_request(
            start_address=start_address,
            quantity=quantity,
        )

        self.serial.reset_input_buffer()

        written = self.serial.write(request)
        self.serial.flush()

        if written != len(request):
            raise RuntimeError(
                f"UART 송신 길이 오류: "
                f"{written}/{len(request)}"
            )

        if DEBUG_MODE:
            print(
                f"TX {len(request)} bytes: "
                f"{request.hex(' ').upper()}"
            )

        # 주소 + 기능코드 + byte count
        header = self.read_exactly(3)

        if len(header) == 0:
            raise RuntimeError(
                f"Slave ID {self.slave_id} 응답 없음"
            )

        if len(header) < 3:
            raise RuntimeError(
                f"응답 헤더 부족: "
                f"{header.hex(' ').upper()}"
            )

        received_slave_id = header[0]
        received_function_code = header[1]

        # Modbus 예외 응답
        if received_function_code & 0x80:
            exception_remainder = self.read_exactly(2)
            exception_frame = header + exception_remainder

            raise RuntimeError(
                "Modbus 예외 응답: "
                f"{exception_frame.hex(' ').upper()}"
            )

        byte_count = header[2]
        remainder = self.read_exactly(byte_count + 2)
        response = header + remainder

        if DEBUG_MODE:
            print(
                f"RX {len(response)} bytes: "
                f"{response.hex(' ').upper()}"
            )

        expected_length = 3 + byte_count + 2

        if len(response) != expected_length:
            raise RuntimeError(
                f"응답 프레임 길이 오류: "
                f"{len(response)}/{expected_length}"
            )

        if received_slave_id != self.slave_id:
            raise RuntimeError(
                f"Slave ID 불일치: "
                f"{received_slave_id}/{self.slave_id}"
            )

        if received_function_code != 0x03:
            raise RuntimeError(
                "기능 코드 불일치: "
                f"0x{received_function_code:02X}"
            )

        expected_byte_count = quantity * 2

        if byte_count != expected_byte_count:
            raise RuntimeError(
                f"데이터 길이 오류: "
                f"{byte_count}/{expected_byte_count}"
            )

        received_crc = int.from_bytes(
            response[-2:],
            byteorder="little",
        )

        calculated_crc = crc16_modbus(
            response[:-2]
        )

        if received_crc != calculated_crc:
            raise RuntimeError(
                "CRC 오류: "
                f"수신=0x{received_crc:04X}, "
                f"계산=0x{calculated_crc:04X}"
            )

        register_data = response[3:-2]

        return list(
            struct.unpack(
                f">{quantity}H",
                register_data,
            )
        )

    def read_registers(
        self,
        start_address: int,
        quantity: int,
    ) -> list[int]:
        """통신 실패 시 설정된 횟수만큼 다시 시도합니다."""

        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self.read_registers_once(
                    start_address=start_address,
                    quantity=quantity,
                )

            except Exception as error:
                last_error = error

                if attempt < MAX_RETRIES:
                    time.sleep(0.2)

        raise RuntimeError(
            f"{MAX_RETRIES}회 통신 실패: {last_error}"
        )

    def read_all(self) -> WeatherReading:
        """레지스터 500~515를 읽고 실제값으로 변환합니다."""

        registers = self.read_registers(
            start_address=FIRST_REGISTER,
            quantity=REGISTER_COUNT,
        )

        if DEBUG_MODE:
            print(f"원시 레지스터: {registers}")

        return decode_registers(registers)


# ============================================================
# 터미널 출력
# ============================================================

def print_reading(
    reading: WeatherReading,
) -> None:
    """센서 측정값을 보기 쉽게 출력합니다."""

    print()
    print("=" * 66)
    print(
        f"종합센서 측정값 | "
        f"{reading.timestamp}"
    )
    print("=" * 66)

    print(
        f"온도           : "
        f"{reading.temperature_c:.1f} ℃"
    )

    print(
        f"습도           : "
        f"{reading.humidity_pct:.1f} %RH"
    )

    print(
        f"CO₂            : "
        f"{reading.co2_ppm} ppm"
    )

    print(
        f"조도           : "
        f"{reading.illuminance_lux} lux"
    )

    print(
        f"일사량         : "
        f"{reading.solar_radiation_wm2} W/m²"
    )

    print(
        f"풍속           : "
        f"{reading.wind_speed_mps:.1f} m/s"
    )

    print(
        f"풍향           : "
        f"{reading.wind_direction_text} "
        f"({reading.wind_direction_deg}°)"
    )

    print(
        f"풍력 등급      : "
        f"{reading.wind_force}"
    )

    print(
        f"대기압         : "
        f"{reading.pressure_kpa:.1f} kPa"
    )

    print(
        f"소음           : "
        f"{reading.noise_db:.1f} dB"
    )

    print(
        f"강우량         : "
        f"{reading.rainfall_mm:.1f} mm"
    )

    print(
        f"전자나침반     : "
        f"{reading.compass_deg:.2f}°"
    )

    if DEBUG_MODE:
        print(
            f"Register 508   : "
            f"{reading.register_508_raw}"
        )


# ============================================================
# 메인 프로그램
# ============================================================

def main() -> None:
    """센서값을 일정 주기로 반복 수집합니다."""

    print("=" * 66)
    print("RS485 종합기상센서 데이터 수집")
    print(f"UART 장치      : {SERIAL_PORT}")
    print(f"Baudrate       : {BAUDRATE}")
    print(f"Slave ID       : {SLAVE_ID}")
    print(f"측정 주기      : {POLL_INTERVAL_SEC}초")
    print("종료           : Ctrl+C")
    print("=" * 66)

    try:
        with WeatherStationSensor(
            port=SERIAL_PORT,
            baudrate=BAUDRATE,
            slave_id=SLAVE_ID,
            timeout=SERIAL_TIMEOUT_SEC,
        ) as sensor:

            print("[성공] UART 및 센서 통신 준비 완료")

            while True:
                cycle_start = time.monotonic()

                try:
                    reading = sensor.read_all()
                    print_reading(reading)

                    # 추후 MQTT 전송 시 사용
                    mqtt_payload = reading.to_mqtt_payload()

                    # 현재 단계에서는 전송하지 않음
                    del mqtt_payload

                except Exception as error:
                    print()
                    print(f"[통신 오류] {error}")

                elapsed = time.monotonic() - cycle_start
                sleep_time = POLL_INTERVAL_SEC - elapsed

                if sleep_time > 0:
                    time.sleep(sleep_time)

    except serial.SerialException as error:
        print(f"[UART 오류] {error}")

    except PermissionError as error:
        print(f"[권한 오류] {error}")

    except KeyboardInterrupt:
        print()
        print("프로그램을 종료합니다.")


if __name__ == "__main__":
    main()
