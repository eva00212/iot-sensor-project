#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 RS485 종합센서 진단 및 데이터 확인

하드웨어 구조
- GPIO14 TX / GPIO15 RX
- /dev/serial0 -> /dev/ttyAMA0
- UART 1개
- RS485 단자 3개 병렬 연결
- 센서별 Modbus Slave ID 사용

저장 기능 없음
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import serial


# ============================================================
# 설정
# ============================================================

PORT = "/dev/serial0"

# 매뉴얼 기본값인 4800을 먼저 확인
BAUDRATES = (4800, 9600, 2400)

# 계획한 ID 1, 2, 3 외에 잘못 설정된 주소도 일부 확인
SLAVE_IDS = tuple(range(1, 11))

# 매뉴얼 내용과 레지스터 표를 모두 반영
FUNCTION_CODES = (0x03, 0x04)

# 자동 탐색용
SCAN_REGISTER = 500
SCAN_REGISTER_COUNT = 1

# 실제 데이터 수집용
DATA_REGISTER_START = 500
DATA_REGISTER_COUNT = 16

# 응답 대기시간
TIMEOUT_SEC = 0.8

# 동일 RS485 버스에서 다음 요청 전 대기
REQUEST_GAP_SEC = 0.30

# 전체 측정 반복 주기
POLL_INTERVAL_SEC = 5.0


@dataclass(frozen=True)
class Device:
    """검색된 Modbus 센서 정보."""

    slave_id: int
    baudrate: int
    function_code: int


# ============================================================
# Modbus RTU 프레임 처리
# ============================================================

def calculate_crc(data: bytes) -> int:
    """Modbus RTU CRC-16 계산."""

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
    slave_id: int,
    function_code: int,
    start_address: int,
    register_count: int,
) -> bytes:
    """FC03 또는 FC04 요청 프레임 생성."""

    body = struct.pack(
        ">BBHH",
        slave_id,
        function_code,
        start_address,
        register_count,
    )

    crc = calculate_crc(body)

    # Modbus RTU CRC는 Low byte, High byte 순서
    return body + struct.pack("<H", crc)


def receive_response(ser: serial.Serial) -> bytes:
    """Modbus RTU 응답 프레임 수신."""

    header = ser.read(3)

    if not header:
        return b""

    if len(header) < 3:
        return header + ser.read(256)

    function_code = header[1]

    # Modbus 예외 응답
    if function_code & 0x80:
        return header + ser.read(2)

    byte_count = header[2]

    # 데이터 바이트 + CRC 2바이트
    return header + ser.read(byte_count + 2)


def check_response(
    response: bytes,
    expected_slave_id: int,
    expected_function_code: int,
    register_count: int,
) -> tuple[bool, str]:
    """수신 프레임 유효성 검사."""

    if not response:
        return False, "응답 없음"

    if len(response) < 5:
        return False, f"프레임 길이 부족: {response.hex(' ')}"

    received_crc = int.from_bytes(
        response[-2:],
        byteorder="little",
    )

    calculated_crc = calculate_crc(response[:-2])

    if received_crc != calculated_crc:
        return (
            False,
            f"CRC 오류: "
            f"수신=0x{received_crc:04X}, "
            f"계산=0x{calculated_crc:04X}",
        )

    if response[0] != expected_slave_id:
        return (
            False,
            f"Slave ID 불일치: {response[0]}",
        )

    received_function = response[1]

    if received_function == (
        expected_function_code | 0x80
    ):
        exception_code = response[2]

        return (
            False,
            f"Modbus 예외 코드: 0x{exception_code:02X}",
        )

    if received_function != expected_function_code:
        return (
            False,
            f"기능 코드 불일치: "
            f"0x{received_function:02X}",
        )

    expected_byte_count = register_count * 2

    if response[2] != expected_byte_count:
        return (
            False,
            f"데이터 길이 불일치: "
            f"{response[2]}/{expected_byte_count}",
        )

    expected_length = 3 + expected_byte_count + 2

    if len(response) != expected_length:
        return (
            False,
            f"프레임 길이 불일치: "
            f"{len(response)}/{expected_length}",
        )

    return True, "정상"


def read_registers(
    ser: serial.Serial,
    slave_id: int,
    function_code: int,
    start_address: int,
    register_count: int,
) -> tuple[
    Optional[list[int]],
    str,
    bytes,
    bytes,
]:
    """Modbus 요청을 전송하고 레지스터를 읽음."""

    request = make_read_request(
        slave_id=slave_id,
        function_code=function_code,
        start_address=start_address,
        register_count=register_count,
    )

    # 이전 통신 데이터 제거
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    ser.write(request)
    ser.flush()

    response = receive_response(ser)

    valid, message = check_response(
        response=response,
        expected_slave_id=slave_id,
        expected_function_code=function_code,
        register_count=register_count,
    )

    if not valid:
        return None, message, request, response

    register_data = response[3:-2]

    registers = list(
        struct.unpack(
            f">{register_count}H",
            register_data,
        )
    )

    return registers, "정상", request, response


# ============================================================
# 센서 자동 검색
# ============================================================

def scan_devices(
    ser: serial.Serial,
) -> list[Device]:
    """Baudrate, Slave ID, FC03/FC04 자동 검색."""

    detected_devices: list[Device] = []
    detected_ids: set[int] = set()

    invalid_data_received = False

    print()
    print("=" * 70)
    print("Modbus RTU 센서 자동 탐색")
    print(f"포트      : {PORT}")
    print(f"Baudrate  : {BAUDRATES}")
    print(f"Slave ID  : 1~10")
    print("기능 코드 : FC03, FC04")
    print("=" * 70)

    for baudrate in BAUDRATES:
        ser.baudrate = baudrate
        time.sleep(0.2)

        print()
        print(f"[검사 중] Baudrate = {baudrate}")

        for slave_id in SLAVE_IDS:
            if slave_id in detected_ids:
                continue

            for function_code in FUNCTION_CODES:
                (
                    registers,
                    message,
                    tx_frame,
                    rx_frame,
                ) = read_registers(
                    ser=ser,
                    slave_id=slave_id,
                    function_code=function_code,
                    start_address=SCAN_REGISTER,
                    register_count=SCAN_REGISTER_COUNT,
                )

                if registers is not None:
                    device = Device(
                        slave_id=slave_id,
                        baudrate=baudrate,
                        function_code=function_code,
                    )

                    detected_devices.append(device)
                    detected_ids.add(slave_id)

                    print(
                        f"[센서 발견] "
                        f"ID={slave_id}, "
                        f"{baudrate}bps, "
                        f"FC=0x{function_code:02X}, "
                        f"REG500={registers[0]}"
                    )

                    break

                # 수신 바이트가 있지만 정상 프레임이 아닌 경우
                if rx_frame:
                    invalid_data_received = True

                    print()
                    print(
                        f"[비정상 데이터 수신] "
                        f"ID={slave_id}, "
                        f"{baudrate}bps, "
                        f"FC=0x{function_code:02X}"
                    )
                    print(f"송신 TX : {tx_frame.hex(' ')}")
                    print(f"수신 RX : {rx_frame.hex(' ')}")
                    print(f"판정    : {message}")

                time.sleep(REQUEST_GAP_SEC)

    print()
    print("=" * 70)

    if detected_devices:
        print(
            f"탐색 완료: "
            f"{len(detected_devices)}대 발견"
        )

        for device in detected_devices:
            print(
                f"ID {device.slave_id}: "
                f"{device.baudrate}bps, "
                f"FC=0x{device.function_code:02X}"
            )

    else:
        print("탐색 완료: 응답 센서 없음")

        if invalid_data_received:
            print(
                "수신 바이트는 있었지만 정상 Modbus 프레임이 "
                "아닙니다."
            )
            print(
                "동일 Slave ID 충돌, 통신속도 불일치 또는 "
                "노이즈 가능성이 있습니다."
            )
        else:
            print("센서에서 수신된 바이트가 없습니다.")
            print(
                "A/B 배선, 센서 전원 또는 RS485 송수신 "
                "전환회로를 확인해야 합니다."
            )

    print("=" * 70)

    return detected_devices


# ============================================================
# 레지스터 변환
# ============================================================

def unsigned_to_signed_16(value: int) -> int:
    """16비트 음수 온도 변환."""

    if value & 0x8000:
        return value - 0x10000

    return value


def decode_sensor_data(
    registers: list[int],
) -> dict[str, object]:
    """레지스터 500~515를 실제 단위로 변환."""

    if len(registers) != 16:
        raise ValueError(
            f"레지스터 개수 오류: {len(registers)}/16"
        )

    lux_high = registers[10]
    lux_low = registers[11]

    illuminance_lux = (
        (lux_high << 16) | lux_low
    )

    return {
        "wind_speed_ms": registers[0] / 10.0,
        "wind_force": registers[1],
        "wind_direction_sector": registers[2],
        "wind_direction_deg": registers[3],

        "humidity_pct": registers[4] / 10.0,
        "temperature_c": (
            unsigned_to_signed_16(registers[5])
            / 10.0
        ),

        "noise_db": registers[6] / 10.0,

        # CO2 타입 센서 기준
        "co2_ppm": registers[7],

        "pm10_ugm3": registers[8],
        "pressure_kpa": registers[9] / 10.0,

        "illuminance_lux": illuminance_lux,
        "illuminance_coarse_lux": (
            registers[12] * 100
        ),

        "rainfall_mm": registers[13] / 10.0,
        "compass_deg": registers[14] / 100.0,
        "solar_radiation_wm2": registers[15],

        "raw_registers": registers,
    }


def print_sensor_data(
    device: Device,
    data: dict[str, object],
) -> None:
    """수신 센서값 출력."""

    print()
    print("-" * 70)
    print(
        f"수신 시각: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(
        f"Slave ID={device.slave_id}, "
        f"{device.baudrate}bps, "
        f"FC=0x{device.function_code:02X}"
    )
    print("-" * 70)

    print(f"온도       : {data['temperature_c']:.1f} ℃")
    print(f"습도       : {data['humidity_pct']:.1f} %RH")
    print(f"CO2        : {data['co2_ppm']} ppm")
    print(f"조도       : {data['illuminance_lux']} lux")
    print(
        f"일사량     : "
        f"{data['solar_radiation_wm2']} W/m²"
    )

    print(
        f"풍속       : "
        f"{data['wind_speed_ms']:.1f} m/s"
    )
    print(f"풍향       : {data['wind_direction_deg']}°")
    print(f"풍력 등급  : {data['wind_force']}")

    print(
        f"대기압     : "
        f"{data['pressure_kpa']:.1f} kPa"
    )
    print(f"소음       : {data['noise_db']:.1f} dB")
    print(f"PM10       : {data['pm10_ugm3']} µg/m³")

    print(
        f"강우량     : "
        f"{data['rainfall_mm']:.1f} mm"
    )
    print(
        f"나침반     : "
        f"{data['compass_deg']:.2f}°"
    )

    print(f"원시값     : {data['raw_registers']}")


# ============================================================
# 반복 데이터 수집
# ============================================================

def poll_devices(
    ser: serial.Serial,
    devices: list[Device],
) -> None:
    """검색된 센서들을 순차적으로 반복 수집."""

    print()
    print("센서값 반복 수집을 시작합니다.")
    print("종료: Ctrl+C")

    while True:
        cycle_start = time.monotonic()
        success_count = 0

        for device in devices:
            ser.baudrate = device.baudrate

            (
                registers,
                message,
                tx_frame,
                rx_frame,
            ) = read_registers(
                ser=ser,
                slave_id=device.slave_id,
                function_code=device.function_code,
                start_address=DATA_REGISTER_START,
                register_count=DATA_REGISTER_COUNT,
            )

            if registers is None:
                print()
                print(
                    f"[통신 실패] ID={device.slave_id}"
                )
                print(f"원인: {message}")
                print(f"TX  : {tx_frame.hex(' ')}")
                print(
                    "RX  : "
                    + (
                        rx_frame.hex(" ")
                        if rx_frame
                        else "없음"
                    )
                )

            else:
                success_count += 1

                data = decode_sensor_data(registers)

                print_sensor_data(
                    device=device,
                    data=data,
                )

            time.sleep(REQUEST_GAP_SEC)

        print()
        print(
            f"정상 수신 센서: "
            f"{success_count}/{len(devices)}대"
        )

        elapsed = time.monotonic() - cycle_start
        remaining_time = POLL_INTERVAL_SEC - elapsed

        if remaining_time > 0:
            time.sleep(remaining_time)


# ============================================================
# 메인 프로그램
# ============================================================

def main() -> None:
    print("=" * 70)
    print("RS485 종합센서 진단 및 데이터 확인")
    print(f"UART 장치: {PORT}")
    print("=" * 70)

    try:
        with serial.Serial(
            port=PORT,
            baudrate=4800,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=TIMEOUT_SEC,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as ser:

            print(
                f"[성공] UART 장치 열림: {ser.port}"
            )

            devices = scan_devices(ser)

            if not devices:
                print()
                print("확인 순서")
                print("1. 센서를 한 대만 연결하고 재실행")
                print("2. 센서 전원 10~30VDC 확인")
                print("3. RS485 A/B를 서로 바꾸어 시험")
                print("4. 센서 ID와 Baudrate 확인")
                print("5. 확장보드 자동 송수신 전환 확인")
                return

            poll_devices(
                ser=ser,
                devices=devices,
            )

    except serial.SerialException as error:
        print(f"[UART 오류] {error}")

    except PermissionError as error:
        print(f"[권한 오류] {error}")
        print(
            "현재 사용자를 dialout 그룹에 추가한 뒤 "
            "재부팅하세요."
        )

    except KeyboardInterrupt:
        print()
        print("사용자 요청으로 종료합니다.")


if __name__ == "__main__":
    main()
