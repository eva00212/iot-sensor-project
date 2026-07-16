"""
server_uploader.py

Publishes payloads to the oneM2M MQTT broker over the Pi's LTE uplink.

Uses a single persistent MQTT connection (not one-shot publish-per-message)
so paho's built-in reconnect-with-backoff can ride out LTE outages without
this module reimplementing reconnect logic. If the connection is down, or a
publish isn't confirmed within publish_timeout_seconds, the payload is
appended to logs/buffer.jsonl instead of blocking the caller — the poll
loop in collector.py must never stall waiting on a flaky link.

Reads connection settings from config/site_config.yaml.
"""

import json
import logging
import threading
import time
from pathlib import Path

import yaml
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "site_config.yaml"

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)

_cfg    = _load_config()
_server = _cfg["server"]
_flush  = _server.get("flush", {})

HOST      = _server["host"]
PORT      = _server["port"]
KEEPALIVE = _server.get("keepalive", 60)
QOS       = _server.get("qos", 1)

# Per-device client_id: a shared literal client_id across multiple deployed
# Pis would collide on the broker now that connections are held open instead
# of one-shot (MQTT only allows one active connection per client_id).
CLIENT_ID = f"{_server.get('client_id', 'rpi-uploader')}-{_cfg.get('site_id', 'unknown')}"

RECONNECT_MIN_DELAY     = _server.get("reconnect_delay", 5)
RECONNECT_MAX_DELAY     = _server.get("max_reconnect_delay", 120)
PUBLISH_TIMEOUT_SECONDS = _server.get("publish_timeout_seconds", 5)

FLUSH_BATCH_SIZE     = _flush.get("batch_size", 20)
FLUSH_PACING_SECONDS = _flush.get("pacing_seconds", 1)

# Caps how large logs/buffer.jsonl can grow during a very long outage.
# Without this, an extended LTE outage would let the buffer file grow
# without bound, indefinitely consuming disk space. Once full, the oldest
# queued payload(s) are dropped to make room for the newest -- keeping
# the most recent state is more useful during a long-running incident
# than preserving the very earliest queued readings.
MAX_BUFFERED_MESSAGES = _flush.get("max_buffered_messages", 10000)

# ── Buffer ────────────────────────────────────────────────────────────────────
BUFFER_PATH  = Path(__file__).parent.parent / "logs" / "buffer.jsonl"
_buffer_lock = threading.Lock()

# ── MQTT client ───────────────────────────────────────────────────────────────
_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
_client.reconnect_delay_set(min_delay=RECONNECT_MIN_DELAY, max_delay=RECONNECT_MAX_DELAY)


def _on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("Uploader connected to %s:%d as '%s'", HOST, PORT, CLIENT_ID)
        # Drain anything queued during the outage as soon as the link is back,
        # rather than waiting for the periodic flush. Run in its own thread so
        # a slow flush doesn't block paho's network loop.
        threading.Thread(target=flush_buffer, daemon=True).start()
    else:
        logger.error("Uploader connect failed: %s", reason_code)


def _on_disconnect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        logger.warning(
            "Uploader disconnected (rc=%s) — likely LTE link down. "
            "Reconnecting with backoff (%ds..%ds).",
            reason_code, RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY,
        )


_client.on_connect    = _on_connect
_client.on_disconnect = _on_disconnect


def start() -> None:
    """
    Starts the persistent MQTT connection in the background. Non-blocking
    even if the network (e.g. LTE) is not yet available at call time — paho
    resolves DNS and connects on its own loop thread and keeps retrying with
    backoff, so the caller (and the service as a whole) starts up cleanly
    regardless of link state.
    """
    logger.info("Uploader starting - connecting to %s:%d (async)", HOST, PORT)
    _client.connect_async(HOST, PORT, KEEPALIVE)
    _client.loop_start()


def stop() -> None:
    _client.loop_stop()
    _client.disconnect()


def _write_to_buffer(converted: dict) -> None:
    with _buffer_lock:
        lines = BUFFER_PATH.read_text(encoding="utf-8").splitlines() if BUFFER_PATH.exists() else []
        lines.append(json.dumps(converted))

        if len(lines) > MAX_BUFFERED_MESSAGES:
            dropped = len(lines) - MAX_BUFFERED_MESSAGES
            lines = lines[dropped:]
            logger.error(
                "Buffer exceeded %d queued message(s); dropped %d oldest to stay bounded.",
                MAX_BUFFERED_MESSAGES, dropped,
            )

        BUFFER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.warning("Payload queued to %s (LTE link unavailable or publish unconfirmed)", BUFFER_PATH)


def _try_upload(converted: dict) -> bool:
    topic = converted["topic"]
    body  = converted["body"]

    if not _client.is_connected():
        logger.warning("Not connected to broker - skipping live publish, queuing instead")
        return False

    try:
        msg_info = _client.publish(topic, payload=body, qos=QOS)
        msg_info.wait_for_publish(timeout=PUBLISH_TIMEOUT_SECONDS)
    except (ValueError, RuntimeError) as e:
        logger.warning("Publish to %s failed: %s", topic, e)
        return False

    if not msg_info.is_published():
        logger.warning("Publish to %s not confirmed within %ds", topic, PUBLISH_TIMEOUT_SECONDS)
        return False

    logger.info("Published to %s:%d %s", HOST, PORT, topic)
    return True


# ── Public API ────────────────────────────────────────────────────────────────
def upload(converted: dict) -> bool:
    """
    Publish to the oneM2M MQTT broker. Never blocks waiting on the network
    beyond publish_timeout_seconds — on any failure (disconnected, timeout,
    unconfirmed) the payload is queued to buffer.jsonl for later retry via
    flush_buffer(), instead of retrying inline in the caller's thread.

    Returns True on confirmed delivery, False if buffered.
    """
    if _try_upload(converted):
        return True

    _write_to_buffer(converted)
    return False


_flush_lock = threading.Lock()  # ensures only one flush pass runs at a time


def flush_buffer() -> None:
    """
    Retransmit payloads queued in buffer.jsonl, one at a time, removing each
    line from disk immediately after its confirmed send (rather than
    rewriting the whole file once at the end). This bounds a crash mid-flush
    to at most one at-risk message instead of the whole backlog, and
    preserves send order. `_buffer_lock` is only held for the brief file
    read/remove operations — never across a network wait or the pacing
    sleep — so a concurrent live upload failure (`_write_to_buffer`) is
    never blocked waiting on an in-progress flush.

    Stops at the first failure (disconnected / unconfirmed) rather than
    hammering a marginal link — the periodic scheduler in collector.py and
    the on-reconnect trigger here will both retry later. Guarded by
    `_flush_lock` so overlapping triggers (periodic + on-reconnect) can't
    both send the same queued message.
    """
    if not _flush_lock.acquire(blocking=False):
        logger.debug("Flush already in progress; skipping this trigger.")
        return
    try:
        _do_flush()
    finally:
        _flush_lock.release()


def _do_flush() -> None:
    with _buffer_lock:
        if not BUFFER_PATH.exists():
            return
        lines = BUFFER_PATH.read_text(encoding="utf-8").splitlines()

    if not lines:
        return

    to_process = lines[:FLUSH_BATCH_SIZE]
    logger.info("Flushing up to %d of %d buffered payload(s)...", len(to_process), len(lines))

    sent = 0
    for line in to_process:
        if not line.strip():
            _remove_buffered_line(line)
            continue

        try:
            converted = json.loads(line)
        except json.JSONDecodeError as e:
            logger.error("Discarding malformed buffer entry: %s", e)
            _remove_buffered_line(line)
            continue

        if not _try_upload(converted):
            logger.warning("Retransmit failed; stopping this flush pass - remaining payload(s) stay queued.")
            break

        _remove_buffered_line(line)
        sent += 1
        logger.info("Retransmitted queued payload to %s", converted.get("topic"))
        time.sleep(FLUSH_PACING_SECONDS)

    if sent:
        logger.info("Buffer flush: %d payload(s) retransmitted.", sent)


def _remove_buffered_line(line_to_remove: str) -> None:
    """
    Removes the first occurrence of `line_to_remove` from buffer.jsonl,
    re-reading the file under the lock so any lines appended concurrently
    by a live `_write_to_buffer` call (from a failed live upload happening
    during this flush pass) are preserved rather than clobbered.
    """
    with _buffer_lock:
        if not BUFFER_PATH.exists():
            return
        current = BUFFER_PATH.read_text(encoding="utf-8").splitlines()
        try:
            current.remove(line_to_remove)
        except ValueError:
            return  # already removed
        if current:
            BUFFER_PATH.write_text("\n".join(current) + "\n", encoding="utf-8")
        else:
            BUFFER_PATH.unlink(missing_ok=True)
