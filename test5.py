#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 5 RS485 소프트웨어 UART

제작 보드의 TX/RX 역접속을 펌웨어로 우회합니다.

보드 연결
- GPIO15 → TXD_SENSOR → U2 DI
- GPIO14 ← RXD_SENSOR ← U2 RO

통신
- Modbus RTU
- Slave ID 1
- 4800 bps
- 8N1
- FC03
- 습도/온도 레지스터 504~505
"""

from __future__ import annotations

import gc
import glob
import os
import struct
import time
from pathlib import Path
from typing import Optional

import lgpio


# ============================================================
# GPIO 및 통신 설정
# ============================================================

# 제작 보드에 맞춰 방향을 반대로 지정
TX_GPIO = 15
RX_GPIO = 14

BAUDRATE = 4800
BIT_TIME_NS = round(1_000_000_000 / BAUDRATE)

SLAVE_ID = 1
FUNCTION_CODE = 0x03

REGISTER_START = 504
REGISTER_COUNT = 2

RESPONSE_TIMEOUT_SEC = 2.0
POLL_INTERVAL_SEC = 3.0


# ============================================================
# RP1 GPIO 칩 검색
# ============================================================

def find_rp1_gpiochip() -> int:
    """pinctrl-rp1 GPIO 칩 번호를 검색합니다."""

    paths = glob.glob(
        "/sys/class/gpio/gpiochip*/label"
    )

    for label_path in paths:
        try:
            label = Path(label_path).read_text().strip()

            if "rp1" in label.lower():
                chip_name = Path(label_path).parent.name
                return int(chip_name.replace("gpiochip", ""))

        except (OSError, ValueError):
            continue

    # Raspberry Pi OS에서 일반적으로 gpiochip0
    return 0


# ============================================================
# 시간 제어
# ============================================================

def wait_until_ns(target_ns: int) -> None:
    """
    지정된 절대 시각까지 대기합니다.

    4800bps에서는 한 비트가 약 208.3µs이므로
    time.sleep 대신 busy wait를 사용합니다.
    """

    while time.monotonic_ns() < target_ns:
        pass


# ============================================================
# Software UART
# ============================================================

class SoftwareUART:
    """GPIO 기반 4800bps 소프트웨어 UART."""

    def __init__(
        self,
        gpiochip: int,
        tx_gpio: int,
        rx_gpio: int,
    ) -> None:

        self.gpiochip_number = gpiochip
        self.tx_gpio = tx_gpio
        self.rx_gpio = rx_gpio

        self.handle = lgpio.gpiochip_open(
            gpiochip
        )

        # UART 유휴 상태는 HIGH
        lgpio.gpio_claim_output(
            self.handle,
            self.tx_gpio,
            1,
        )

        lgpio.gpio_claim_input(
            self.handle,
            self.rx_gpio,
        )

    def close(self) -> None:
        """GPIO를 정리합니다."""

        try:
            lgpio.gpio_write(
                self.handle,
                self.tx_gpio,
                1,
            )
        except Exception:
            pass

        try:
            lgpio.gpio_free(
                self.handle,
                self.tx_gpio,
            )
        except Exception:
            pass

        try:
            lgpio.gpio_free(
                self.handle,
                self.rx_gpio,
            )
        except Exception:
            pass

        lgpio.gpiochip_close(self.handle)

    def send_byte(self, value: int) -> None:
        """
        8N1 형식으로 바이트를 송신합니다.

        순서:
        Start 0
        Data bit 0~7, LSB first
        Stop 1
        """

        bits = [0]

        for bit_index in range(8):
            bits.append(
                (value >> bit_index) & 0x01
            )

        bits.append(1)

        deadline = time.monotonic_ns()

        for bit in bits:
            lgpio.gpio_write(
                self.handle,
                self.tx_gpio,
                bit,
            )

            deadline += BIT_TIME_NS
            wait_until_ns(deadline)

    def send(self, data: bytes) -> None:
        """바이트 배열을 연속 송신합니다."""

        # Modbus RTU 요청 전 무통신 구간 확보
        lgpio.gpio_write(
            self.handle,
            self.tx_gpio,
            1,
        )

        time.sleep(0.01)

        for value in data:
            self.send_byte(value)

        # 송신 완료 후 수신 모드가 되도록 HIGH 유지
        lgpio.gpio_write(
            self.handle,
            self.tx_gpio,
            1,
        )

    def read_byte(
        self,
        deadline_ns: int,
    ) -> Optional[int]:
        """
        UART 바이트 한 개를 수신합니다.

        Start bit의 하강을 찾은 뒤 각 비트 중앙에서
        GPIO14 값을 읽습니다.
        """

        # Start bit LOW 대기
        while time.monotonic_ns() < deadline_ns:
            if lgpio.gpio_read(
                self.handle,
                self.rx_gpio,
            ) == 0:
                start_ns = time.monotonic_ns()
                break
        else:
            return None

        value = 0

        # Start bit 시작 후 1.5 bit 지점이 첫 데이터 비트 중앙
        sample_ns = (
            start_ns
            + BIT_TIME_NS
            + BIT_TIME_NS // 2
        )

        for bit_index in range(8):
            wait_until_ns(sample_ns)

            bit = lgpio.gpio_read(
                self.handle,
                self.rx_gpio,
            )

            if bit:
                value |= 1 << bit_index

            sample_ns += BIT_TIME_NS

        # Stop bit 중앙
        wait_until_ns(sample_ns)

        stop_bit = lgpio.gpio_read(
            self.handle,
            self.rx_gpio,
        )

        if stop_bit != 1:
            print("[경고] UART Stop bit 오류")

        return value

    def read_modbus_frame(
        self,
        timeout_sec: float,
    ) -> bytes:
        """Modbus RTU 응답 한 프레임을 수신합니다."""

        deadline_ns = (
            time.monotonic_ns()
            + int(timeout_sec * 1_000_000_000)
        )

        response = bytearray()

        # 주소, 기능 코드, 바이트 수
        for _ in range(3):
            value = self.read_byte(deadline_ns)

            if value is None:
                return bytes(response)

            response.append(value)

        function_code = response[1]

        if function_code & 0x80:
            # 예외 코드 1바이트 + CRC 2바이트
            expected_length = 5
        else:
            byte_count = response[2]
            expected_length = 3 + byte_count + 2

        while len(response) < expected_length:
            value = self.read_byte(deadline_ns)

            if value is None:
                break

            response.append(value)

        return bytes(response)


# ============================================================
# Modbus RTU
# ============================================================

def calculate_crc(data: bytes) -> int:
    """Modbus RTU CRC16 계산."""

    crc = 0xFFFF

    for value in data:
        crc ^= value

        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1

    return crc & 0xFFFF


def make_request() -> bytes:
    """FC03, 레지스터 504~505 요청 생성."""

    body = struct.pack(
        ">BBHH",
        SLAVE_ID,
        FUNCTION_CODE,
        REGISTER_START,
        REGISTER_COUNT,
    )

    crc = calculate_crc(body)

    return body + struct.pack("<H", crc)


def validate_response(
    response: bytes,
) -> tuple[bool, str]:
    """Modbus 응답을 검증합니다."""

    if not response:
        return False, "수신 데이터 없음"

    if len(response) < 5:
        return (
            False,
            f"응답 길이 부족: {len(response)}바이트",
        )

    received_crc = int.from_bytes(
        response[-2:],
        byteorder="little",
    )

    calculated_crc = calculate_crc(
        response[:-2]
    )

    if received_crc != calculated_crc:
        return (
            False,
            f"CRC 오류: 수신=0x{received_crc:04X}, "
            f"계산=0x{calculated_crc:04X}",
        )

    if response[0] != SLAVE_ID:
        return (
            False,
            f"Slave ID 불일치: {response[0]}",
        )

    if response[1] == 0x83:
        return (
            False,
            f"Modbus 예외 코드: 0x{response[2]:02X}",
        )

    if response[1] != FUNCTION_CODE:
        return (
            False,
            f"기능 코드 불일치: 0x{response[1]:02X}",
        )

    if response[2] != REGISTER_COUNT * 2:
        return (
            False,
            f"데이터 길이 오류: {response[2]}",
        )

    return True, "정상"


def uint16_to_int16(value: int) -> int:
    """온도 음수값을 변환합니다."""

    if value & 0x8000:
        return value - 0x10000

    return value


def print_temperature_humidity(
    response: bytes,
) -> None:
    """응답에서 습도와 온도를 출력합니다."""

    humidity_raw = int.from_bytes(
        response[3:5],
        byteorder="big",
    )

    temperature_raw = int.from_bytes(
        response[5:7],
        byteorder="big",
    )

    humidity = humidity_raw / 10.0

    temperature = (
        uint16_to_int16(temperature_raw)
        / 10.0
    )

    print("[수신 성공]")
    print(f"습도 : {humidity:.1f} %RH")
    print(f"온도 : {temperature:.1f} ℃")


# ============================================================
# 메인
# ============================================================

def main() -> None:
    """소프트웨어 UART Modbus 통신 테스트."""

    gpiochip = find_rp1_gpiochip()
    request = make_request()

    print("=" * 65)
    print("RS485 소프트웨어 UART 통신 테스트")
    print(f"GPIO chip : /dev/gpiochip{gpiochip}")
    print(f"TX        : GPIO{TX_GPIO}")
    print(f"RX        : GPIO{RX_GPIO}")
    print(f"Baudrate  : {BAUDRATE}")
    print(f"Slave ID  : {SLAVE_ID}")
    print(
        f"TX frame  : "
        f"{request.hex(' ').upper()}"
    )
    print("종료      : Ctrl+C")
    print("=" * 65)

    # Python GC로 인한 통신 지연 방지
    gc.disable()

    # 가능하면 특정 CPU 코어에 고정
    try:
        os.sched_setaffinity(0, {3})
    except (AttributeError, OSError):
        pass

    # sudo 실행 시 실시간 우선순위 적용
    try:
        os.sched_setscheduler(
            0,
            os.SCHED_FIFO,
            os.sched_param(50),
        )
        print("[설정] 실시간 스케줄링 적용")
    except (PermissionError, OSError):
        print(
            "[경고] 실시간 스케줄링 미적용. "
            "sudo 실행을 권장합니다."
        )

    uart = SoftwareUART(
        gpiochip=gpiochip,
        tx_gpio=TX_GPIO,
        rx_gpio=RX_GPIO,
    )

    try:
        while True:
            print()
            print(
                f"[송신] "
                f"{request.hex(' ').upper()}"
            )

            uart.send(request)

            response = uart.read_modbus_frame(
                timeout_sec=RESPONSE_TIMEOUT_SEC
            )

            if response:
                print(
                    f"[수신] {len(response)}바이트: "
                    f"{response.hex(' ').upper()}"
                )
            else:
                print("[수신] 0바이트")

            valid, message = validate_response(
                response
            )

            if valid:
                print_temperature_humidity(
                    response
                )
            else:
                print(f"[실패] {message}")

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print()
        print("프로그램을 종료합니다.")

    finally:
        uart.close()
        gc.enable()


if __name__ == "__main__":
    main()
