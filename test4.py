#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 + RS485 확장보드
종합기상센서 1대 통신 확인

현재 구성
- UART: GPIO14 TX / GPIO15 RX
- Linux 장치: /dev/serial0 -> /dev/ttyAMA0
- RS485 버스 1개
- 센서 1대
- Slave ID 1
- 저장 기능 없음
"""

from __future__ import annotations

import os
import struct
import time
from typing import Optional

import serial


# ============================================================
# 통신 설정
# ============================================================

PORT = "/dev/serial0"

SLAVE_ID = 1
BAUDRATE = 4800

BYTESIZE = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOPBITS = serial.STOPBITS_ONE

RESPONSE_TIMEOUT_SEC = 2.0
POLL_INTERVAL_SEC = 3.0
MAX_RETRIES = 3


# ============================================================
# Modbus CRC
# ============================================================

def calculate_crc(data: bytes) -> int:
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


def make_read_request(
    function_code: int,
    start_address: int,
    register_count: int,
) -> bytes:
    """Modbus FC03 또는 FC04 요청 프레임을 생성합니다."""

    frame_without_crc = struct.pack(
        ">BBHH",
        SLAVE_ID,
        function_code,
        start_address,
        register_count,
    )

    crc = calculate_crc(frame_without_crc)

    # Modbus CRC는 Low Byte, High Byte 순서
    return frame_without_crc + struct.pack("<H", crc)


# ============================================================
# Modbus 응답 수신
# ============================================================

def receive_modbus_frame(
    uart: serial.Serial,
) -> bytes:
    """
    Modbus RTU 응답 프레임을 수신합니다.

    정상 응답:
        Slave ID 1바이트
        Function 1바이트
        Byte count 1바이트
        Data N바이트
        CRC 2바이트
    """

    header = uart.read(3)

    if not header:
        return b""

    if len(header) < 3:
        return header

    function_code = header[1]

    # Modbus 예외 응답
    if function_code & 0x80:
        remainder = uart.read(2)
        return header + remainder

    byte_count = header[2]

    # 데이터와 CRC 수신
    remainder = uart.read(byte_count + 2)

    return header + remainder


def validate_response(
    response: bytes,
    expected_function_code: int,
    expected_register_count: int,
) -> tuple[bool, str]:
    """Modbus 응답의 ID, 기능 코드, 길이 및 CRC를 검사합니다."""

    if not response:
        return False, "센서 응답 0바이트"

    if len(response) < 5:
        return (
            False,
            f"응답 길이 부족: {len(response)}바이트",
        )

    received_crc = int.from_bytes(
        response[-2:],
        byteorder="little",
    )

    calculated_crc = calculate_crc(response[:-2])

    if received_crc != calculated_crc:
        return (
            False,
            f"CRC 불일치: 수신=0x{received_crc:04X}, "
            f"계산=0x{calculated_crc:04X}",
        )

    if response[0] != SLAVE_ID:
        return (
            False,
            f"Slave ID 불일치: {response[0]}",
        )

    received_function_code = response[1]

    if received_function_code == (
        expected_function_code | 0x80
    ):
        exception_code = response[2]

        return (
            False,
            f"Modbus 예외 응답: 0x{exception_code:02X}",
        )

    if received_function_code != expected_function_code:
        return (
            False,
            f"기능 코드 불일치: "
            f"0x{received_function_code:02X}",
        )

    expected_byte_count = expected_register_count * 2

    if response[2] != expected_byte_count:
        return (
            False,
            f"데이터 길이 불일치: "
            f"{response[2]}/{expected_byte_count}",
        )

    expected_frame_length = (
        3 + expected_byte_count + 2
    )

    if len(response) != expected_frame_length:
        return (
            False,
            f"프레임 길이 불일치: "
            f"{len(response)}/{expected_frame_length}",
        )

    return True, "정상"


def read_registers(
    uart: serial.Serial,
    function_code: int,
    start_address: int,
    register_count: int,
) -> Optional[list[int]]:
    """센서에 요청을 전송하고 레지스터를 반환합니다."""

    request = make_read_request(
        function_code=function_code,
        start_address=start_address,
        register_count=register_count,
    )

    for attempt in range(1, MAX_RETRIES + 1):
        uart.reset_input_buffer()

        written = uart.write(request)
        uart.flush()

        print()
        print(
            f"[송신 {attempt}/{MAX_RETRIES}] "
            f"{written}바이트"
        )
        print(f"TX: {request.hex(' ').upper()}")

        response = receive_modbus_frame(uart)

        if response:
            print(
                f"RX: {response.hex(' ').upper()} "
                f"({len(response)}바이트)"
            )
        else:
            print("RX: 없음, 0바이트")

        valid, message = validate_response(
            response=response,
            expected_function_code=function_code,
            expected_register_count=register_count,
        )

        if valid:
            register_bytes = response[3:-2]

            registers = list(
                struct.unpack(
                    f">{register_count}H",
                    register_bytes,
                )
            )

            return registers

        print(f"[실패] {message}")

        if attempt < MAX_RETRIES:
            time.sleep(0.5)

    return None


# ============================================================
# 센서 데이터 변환
# ============================================================

def uint16_to_int16(value: int) -> int:
    """16비트 2의 보수 온도를 signed 값으로 변환합니다."""

    value &= 0xFFFF

    if value >= 0x8000:
        return value - 0x10000

    return value


def decode_all_registers(
    registers: list[int],
) -> dict[str, float | int]:
    """레지스터 500~515를 실제 단위로 변환합니다."""

    if len(registers) != 16:
        raise ValueError(
            f"레지스터 개수가 올바르지 않습니다: "
            f"{len(registers)}/16"
        )

    lux_high = registers[10]
    lux_low = registers[11]

    illuminance_lux = (
        (lux_high << 16) | lux_low
    )

    return {
        # 500
        "wind_speed_ms": registers[0] / 10.0,

        # 501
        "wind_force": registers[1],

        # 502
        "wind_direction_sector": registers[2],

        # 503
        "wind_direction_deg": registers[3],

        # 504
        "humidity_pct": registers[4] / 10.0,

        # 505
        "temperature_c": (
            uint16_to_int16(registers[5]) / 10.0
        ),

        # 506
        "noise_db": registers[6] / 10.0,

        # 507, CO2 옵션 센서 기준
        "co2_ppm": registers[7],

        # 508
        "pm10_ugm3": registers[8],

        # 509
        "pressure_kpa": registers[9] / 10.0,

        # 510, 511
        "illuminance_lux": illuminance_lux,

        # 512
        "illuminance_coarse_lux": (
            registers[12] * 100
        ),

        # 513
        "rainfall_mm": registers[13] / 10.0,

        # 514
        "compass_deg": registers[14] / 100.0,

        # 515
        "solar_radiation_wm2": registers[15],
    }


def print_sensor_data(
    registers: list[int],
) -> None:
    """수신한 종합센서 데이터를 터미널에 출력합니다."""

    data = decode_all_registers(registers)

    print()
    print("=" * 68)
    print("종합센서 수신 성공")
    print("=" * 68)

    print(f"온도       : {data['temperature_c']:.1f} ℃")
    print(f"습도       : {data['humidity_pct']:.1f} %RH")
    print(f"CO₂        : {data['co2_ppm']} ppm")
    print(f"조도       : {data['illuminance_lux']} lux")

    print(
        f"일사량     : "
        f"{data['solar_radiation_wm2']} W/m²"
    )

    print(
        f"풍속       : "
        f"{data['wind_speed_ms']:.1f} m/s"
    )

    print(
        f"풍향       : "
        f"{data['wind_direction_deg']}°"
    )

    print(f"풍력 등급  : {data['wind_force']}")

    print(
        f"대기압     : "
        f"{data['pressure_kpa']:.1f} kPa"
    )

    print(
        f"소음       : "
        f"{data['noise_db']:.1f} dB"
    )

    print(
        f"PM10       : "
        f"{data['pm10_ugm3']} µg/m³"
    )

    print(
        f"강우량     : "
        f"{data['rainfall_mm']:.1f} mm"
    )

    print(
        f"나침반     : "
        f"{data['compass_deg']:.2f}°"
    )

    print(f"원시값     : {registers}")


# ============================================================
# 메인 프로그램
# ============================================================

def main() -> None:
    """종합센서 단일 장치 통신 테스트."""

    actual_port = os.path.realpath(PORT)

    print("=" * 68)
    print("RS485 종합센서 단일 장치 테스트")
    print(f"UART 별칭     : {PORT}")
    print(f"실제 UART     : {actual_port}")
    print(f"Baudrate      : {BAUDRATE}")
    print(f"Slave ID      : {SLAVE_ID}")
    print("데이터 형식   : 8N1")
    print("종료          : Ctrl+C")
    print("=" * 68)

    try:
        with serial.Serial(
            port=PORT,
            baudrate=BAUDRATE,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=RESPONSE_TIMEOUT_SEC,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as uart:

            print(f"[성공] UART 열림: {uart.port}")

            time.sleep(0.5)

            # ------------------------------------------------
            # 1단계: 매뉴얼의 온·습도 예제와 동일한 주소 확인
            # Register 504: 습도
            # Register 505: 온도
            # ------------------------------------------------

            print()
            print("=" * 68)
            print("1단계: 온·습도 레지스터 504~505 통신 확인")
            print("=" * 68)

            temperature_humidity = read_registers(
                uart=uart,
                function_code=0x03,
                start_address=504,
                register_count=2,
            )

            # FC03에서 응답이 없을 경우 FC04 한 번 확인
            if temperature_humidity is None:
                print()
                print(
                    "FC03 응답이 없어 FC04로 한 번 더 "
                    "확인합니다."
                )

                temperature_humidity = read_registers(
                    uart=uart,
                    function_code=0x04,
                    start_address=504,
                    register_count=2,
                )

            if temperature_humidity is None:
                print()
                print("=" * 68)
                print("센서 응답을 받지 못했습니다.")
                print("=" * 68)
                print("코드 설정:")
                print(f"  포트     : {PORT}")
                print(f"  실제장치 : {actual_port}")
                print(f"  속도     : {BAUDRATE}")
                print(f"  ID       : {SLAVE_ID}")
                print("  주소     : 504")
                print("  개수     : 2")
                print()
                print(
                    "이 결과가 나오면 레지스터 해석 문제가 "
                    "아니라 RS485 물리 연결을 확인해야 합니다."
                )
                return

            humidity = temperature_humidity[0] / 10.0

            temperature = (
                uint16_to_int16(
                    temperature_humidity[1]
                )
                / 10.0
            )

            print()
            print("[온·습도 수신 성공]")
            print(f"습도: {humidity:.1f} %RH")
            print(f"온도: {temperature:.1f} ℃")

            # ------------------------------------------------
            # 2단계: 전체 레지스터 반복 수집
            # ------------------------------------------------

            print()
            print("=" * 68)
            print("2단계: 전체 레지스터 500~515 반복 수집")
            print("=" * 68)

            while True:
                registers = read_registers(
                    uart=uart,
                    function_code=0x03,
                    start_address=500,
                    register_count=16,
                )

                if registers is not None:
                    print_sensor_data(registers)
                else:
                    print()
                    print(
                        "[전체 데이터 실패] "
                        "다음 주기에 다시 시도합니다."
                    )

                time.sleep(POLL_INTERVAL_SEC)

    except serial.SerialException as error:
        print(f"[UART 오류] {error}")

    except PermissionError as error:
        print(f"[권한 오류] {error}")

    except KeyboardInterrupt:
        print()
        print("프로그램을 종료합니다.")


if __name__ == "__main__":
    main()
