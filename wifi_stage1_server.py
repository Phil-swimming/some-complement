#!/usr/bin/env python3
import argparse
import queue
import shlex
import socket
import struct
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import scrolledtext
    from tkinter import ttk
except ImportError:
    tk = None
    scrolledtext = None
    ttk = None


SOF = 0x55AA
PROTO_VER = 1
TCP_PORT = 5500
UDP_PORT = 5501

MSG_HELLO = 0x01
MSG_HEARTBEAT = 0x02
MSG_SET_INPUT = 0x10
MSG_SET_TUNING = 0x11
MSG_SET_ENABLE = 0x12
MSG_SET_MODE = 0x13
MSG_ACK = 0x80
MSG_TELEMETRY = 0x90

TUNE_FLAG_KP = 1 << 0
TUNE_FLAG_KW = 1 << 1
TUNE_FLAG_TFF = 1 << 2
TUNE_FLAG_SPEED_STEP = 1 << 3
TUNE_FLAG_W_LIMIT = 1 << 4
TUNE_FLAG_W_RAMP_STEP = 1 << 5
TUNE_FLAG_T_LIMIT = 1 << 6

INPUT_SOURCES = {
    "kbd": 0,
    "keyboard": 0,
    "rc": 1,
    "ibus": 1,
    "wifi": 2,
}

CTRL_MODES = {
    "indep": 0,
    "sync": 1,
    "diff": 2,
}

SELECTIONS = {
    "m0": 0,
    "m1": 1,
    "both": 2,
}

TARGET_MASKS = {
    "m0": 0x01,
    "m1": 0x02,
    "both": 0x03,
}

INPUT_SOURCE_NAMES = {
    0: "kbd",
    1: "rc",
    2: "wifi",
}

CTRL_MODE_NAMES = {
    0: "indep",
    1: "sync",
    2: "diff",
}

SELECTION_NAMES = {
    0: "m0",
    1: "m1",
    2: "both",
}


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def read_le_u16(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<H", blob, offset)[0]


def read_le_u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def read_le_f32(blob: bytes, offset: int) -> float:
    return struct.unpack_from("<f", blob, offset)[0]


def decode_ip(raw: bytes) -> str:
    return ".".join(str(x) for x in raw)


def parse_hello_payload(payload: bytes) -> dict:
    return {
        "tick_ms": read_le_u32(payload, 0),
        "backend": payload[4],
        "link_state": payload[5],
        "tcp_connected": payload[6],
        "board": payload[8:24].split(b"\0", 1)[0].decode("ascii", errors="replace"),
        "telemetry_ms": read_le_u16(payload, 24),
        "server_port": read_le_u16(payload, 26),
        "server_ip": decode_ip(payload[28:32]),
    }


def parse_telemetry(payload: bytes) -> dict:
    offset = 0
    data = {}

    data["tick_ms"] = read_le_u32(payload, offset)
    offset += 4
    data["backend"] = payload[offset]
    data["link_state"] = payload[offset + 1]
    data["tcp_connected"] = payload[offset + 2]
    data["hello_sent"] = payload[offset + 3]
    offset += 4

    data["ap6181_state"] = 0
    data["ap6181_last_err"] = 0
    data["profile_cfg_ok"] = 0
    data["profile_cfg_err"] = 0
    if len(payload) >= 196:
        data["ap6181_state"] = payload[offset]
        data["ap6181_last_err"] = payload[offset + 1]
        data["profile_cfg_ok"] = payload[offset + 2]
        data["profile_cfg_err"] = payload[offset + 3]
        offset += 4

    data["local_ip"] = decode_ip(payload[offset:offset + 4])
    offset += 4
    data["server_ip"] = decode_ip(payload[offset:offset + 4])
    offset += 4

    data["server_port"] = read_le_u16(payload, offset)
    offset += 2
    data["last_tx_len"] = read_le_u16(payload, offset)
    offset += 2

    data["input_src"] = payload[offset]
    data["ctrl_mode"] = payload[offset + 1]
    data["sel"] = payload[offset + 2]
    data["en_mask"] = payload[offset + 3]
    offset += 4

    data["in_throttle"] = read_le_f32(payload, offset)
    offset += 4
    data["in_steer"] = read_le_f32(payload, offset)
    offset += 4
    data["speed_step"] = read_le_f32(payload, offset)
    offset += 4
    data["w_limit"] = read_le_f32(payload, offset)
    offset += 4
    data["ramp_step"] = read_le_f32(payload, offset)
    offset += 4
    data["t_limit"] = read_le_f32(payload, offset)
    offset += 4

    cmds = []
    for _ in range(2):
        motor_id = payload[offset]
        offset += 4
        kp = read_le_f32(payload, offset)
        offset += 4
        kw = read_le_f32(payload, offset)
        offset += 4
        w_target = read_le_f32(payload, offset)
        offset += 4
        w_cmd = read_le_f32(payload, offset)
        offset += 4
        t_ff = read_le_f32(payload, offset)
        offset += 4
        t_jog = read_le_f32(payload, offset)
        offset += 4
        t_cmd = read_le_f32(payload, offset)
        offset += 4
        cmds.append((motor_id, kp, kw, w_target, w_cmd, t_ff, t_jog, t_cmd))
    data["cmds"] = cmds

    fbks = []
    for _ in range(2):
        motor_id = payload[offset]
        mode = payload[offset + 1]
        merr = payload[offset + 2]
        offset += 4
        temp_c = struct.unpack_from("<h", payload, offset)[0]
        offset += 2
        force = read_le_u16(payload, offset)
        offset += 2
        pos_rad = read_le_f32(payload, offset)
        offset += 4
        w_rad_s = read_le_f32(payload, offset)
        offset += 4
        t_nm = read_le_f32(payload, offset)
        offset += 4
        last_rx_tick = read_le_u32(payload, offset)
        offset += 4
        fbks.append((motor_id, mode, merr, temp_c, force, pos_rad, w_rad_s, t_nm, last_rx_tick))
    data["fbks"] = fbks

    data["rs485_tx_cnt"] = read_le_u32(payload, offset)
    offset += 4
    data["rs485_tx_err_cnt"] = read_le_u32(payload, offset)
    offset += 4
    data["rs485_rx_ok_cnt"] = read_le_u32(payload, offset)
    offset += 4
    data["rs485_rx_crc_err_cnt"] = read_le_u32(payload, offset)
    offset += 4
    data["ibus_ok"] = read_le_u32(payload, offset)
    offset += 4
    data["ibus_bad"] = read_le_u32(payload, offset)
    offset += 4
    data["recover_req"] = read_le_u32(payload, offset)
    offset += 4
    data["recover_run"] = read_le_u32(payload, offset)

    return data


def telemetry_summary_from_data(data: dict) -> str:
    cmd0 = data["cmds"][0]
    fbk0 = data["fbks"][0]
    fbk1 = data["fbks"][1]

    return (
        f"tick={data['tick_ms']}ms backend={data['backend']} link={data['link_state']} "
        f"tcp={data['tcp_connected']} hello={data['hello_sent']} "
        f"ap={data['ap6181_state']} err=0x{data['ap6181_last_err']:02X} "
        f"cfg={'OK' if data['profile_cfg_ok'] else 'E'+str(data['profile_cfg_err'])} "
        f"local={data['local_ip']} server={data['server_ip']}:{data['server_port']} "
        f"last_tx_len={data['last_tx_len']} src={data['input_src']} mode={data['ctrl_mode']} "
        f"sel={data['sel']} en=0x{data['en_mask']:02X} "
        f"thr={data['in_throttle']:+.2f} steer={data['in_steer']:+.2f} "
        f"speed={data['speed_step']:.1f}/{data['w_limit']:.1f} "
        f"ramp={data['ramp_step']:.2f} tlim={data['t_limit']:.2f} "
        f"m0_cmd(kp={cmd0[1]:.2f},kw={cmd0[2]:.2f},w={cmd0[4]:+.2f},tff={cmd0[5]:+.2f}) "
        f"m0_fbk(w={fbk0[6]:+.2f},t={fbk0[7]:+.2f},temp={fbk0[3]}) "
        f"m1_fbk(w={fbk1[6]:+.2f},t={fbk1[7]:+.2f},temp={fbk1[3]}) "
        f"rs485(ok={data['rs485_rx_ok_cnt']},crc={data['rs485_rx_crc_err_cnt']},txe={data['rs485_tx_err_cnt']}) "
        f"ibus(ok={data['ibus_ok']},bad={data['ibus_bad']}) recover={data['recover_req']}/{data['recover_run']}"
    )


def telemetry_summary(payload: bytes) -> str:
    return telemetry_summary_from_data(parse_telemetry(payload))


def bridge_telemetry_summary(payload: bytes) -> str:
    offset = 0

    tick_ms = read_le_u32(payload, offset)
    offset += 4
    backend = payload[offset]
    link_state = payload[offset + 1]
    tcp_connected = payload[offset + 2]
    hello_sent = payload[offset + 3]
    offset += 4

    local_ip = decode_ip(payload[offset:offset + 4])
    offset += 4
    server_ip = decode_ip(payload[offset:offset + 4])
    offset += 4

    server_port = read_le_u16(payload, offset)
    offset += 2
    last_tx_len = read_le_u16(payload, offset)
    offset += 2

    tx_frame_cnt = read_le_u32(payload, offset)
    offset += 4
    tx_err_cnt = read_le_u32(payload, offset)
    offset += 4
    rx_frame_cnt = read_le_u32(payload, offset)
    offset += 4
    rx_crc_err_cnt = read_le_u32(payload, offset)
    offset += 4
    heartbeat_rx_cnt = read_le_u32(payload, offset)
    offset += 4
    last_rx_seq = read_le_u16(payload, offset)
    offset += 2
    last_rx_type = payload[offset]

    return (
        f"tick={tick_ms}ms backend={backend} link={link_state} tcp={tcp_connected} hello={hello_sent} "
        f"local={local_ip} server={server_ip}:{server_port} last_tx_len={last_tx_len} "
        f"tx={tx_frame_cnt} tx_err={tx_err_cnt} rx={rx_frame_cnt} crc_err={rx_crc_err_cnt} "
        f"hb_rx={heartbeat_rx_cnt} last_rx=(type=0x{last_rx_type:02X},seq={last_rx_seq})"
    )


def hello_summary(payload: bytes) -> str:
    data = parse_hello_payload(payload)
    return (
        f"tick={data['tick_ms']}ms board={data['board']} backend={data['backend']} link={data['link_state']} "
        f"tcp={data['tcp_connected']} telemetry={data['telemetry_ms']}ms "
        f"target={data['server_ip']}:{data['server_port']}"
    )


def ack_summary(payload: bytes) -> str:
    ack_seq = read_le_u16(payload, 0)
    ack_type = payload[2]
    status = payload[3]
    rx_frame_cnt = read_le_u32(payload, 4)
    heartbeat_rx_cnt = read_le_u32(payload, 8)
    return (
        f"ack_seq={ack_seq} ack_type=0x{ack_type:02X} status={status} "
        f"rx_total={rx_frame_cnt} hb_total={heartbeat_rx_cnt}"
    )


def build_frame(msg_type: int, seq: int, payload: bytes) -> bytes:
    header = struct.pack("<HBBHH", SOF, PROTO_VER, msg_type, seq, len(payload))
    frame_wo_crc = header + payload
    crc = crc16_ccitt(frame_wo_crc)
    return frame_wo_crc + struct.pack("<H", crc)


def build_heartbeat_payload(heartbeat_count: int) -> bytes:
    return struct.pack("<II", int(time.time() * 1000) & 0xFFFFFFFF, heartbeat_count)


def build_set_input_payload(throttle: float, steer: float, source: int = 2) -> bytes:
    return struct.pack("<ffBBBB", throttle, steer, source, 0, 0, 0)


def build_set_tuning_payload(field: str, value: float, target_mask: int) -> bytes:
    flags = 0
    kp = 0.0
    kw = 0.0
    t_ff = 0.0
    speed_step = 0.0
    w_limit = 0.0
    w_ramp_step = 0.0
    t_limit = 0.0

    if field == "kp":
        flags = TUNE_FLAG_KP
        kp = value
    elif field == "kw":
        flags = TUNE_FLAG_KW
        kw = value
    elif field == "tff":
        flags = TUNE_FLAG_TFF
        t_ff = value
    elif field == "speed":
        flags = TUNE_FLAG_SPEED_STEP
        speed_step = value
    elif field == "limit":
        flags = TUNE_FLAG_W_LIMIT
        w_limit = value
    elif field == "ramp":
        flags = TUNE_FLAG_W_RAMP_STEP
        w_ramp_step = value
    elif field == "tlimit":
        flags = TUNE_FLAG_T_LIMIT
        t_limit = value
    else:
        raise ValueError(f"unsupported tuning field: {field}")

    return struct.pack(
        "<BBHfffffff",
        target_mask & 0x03,
        flags,
        0,
        kp,
        kw,
        t_ff,
        speed_step,
        w_limit,
        w_ramp_step,
        t_limit,
    )


def build_set_enable_payload(enable_mask: int) -> bytes:
    return struct.pack("<BBBB", enable_mask & 0x03, 0, 0, 0)


def build_set_mode_payload(ctrl_mode: int, selection: int, input_src: int) -> bytes:
    return struct.pack("<BBBB", ctrl_mode, selection, input_src, 0)


class LogController:
    def __init__(self, console_enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._listeners = []
        self._show_ack = True
        self._show_tlm = True
        self._console_enabled = console_enabled

    def add_listener(self, listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def set_visible(self, category: str, enabled: bool) -> None:
        with self._lock:
            if category == "ack":
                self._show_ack = bool(enabled)
            elif category == "tlm":
                self._show_tlm = bool(enabled)
            else:
                raise ValueError(f"unsupported log category: {category}")

    def is_visible(self, category: str) -> bool:
        with self._lock:
            return self._is_visible_locked(category)

    def filters_snapshot(self) -> dict:
        with self._lock:
            return {
                "ack": self._show_ack,
                "tlm": self._show_tlm,
                "console": self._console_enabled,
            }

    def emit(self, category: str, line: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
            visible = self._is_visible_locked(category)
            console_enabled = self._console_enabled

        if visible and console_enabled:
            print(line, flush=True)

        for listener in listeners:
            try:
                listener(category, line, visible)
            except Exception:
                pass

    def _is_visible_locked(self, category: str) -> bool:
        if category == "ack":
            return self._show_ack
        if category == "tlm":
            return self._show_tlm
        return True


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.client_addr = ""
        self.connected = False
        self.board_name = ""
        self.last_hello = ""
        self.last_ack = ""
        self.last_ack_seq = 0
        self.last_tlm = ""
        self.last_tlm_seq = 0
        self.last_tlm_data = None
        self.hold_active = False
        self.hold_throttle = 0.0
        self.hold_steer = 0.0
        self.hold_period_ms = 50

    def set_connected(self, addr) -> None:
        with self._lock:
            self.connected = True
            self.client_addr = f"{addr[0]}:{addr[1]}"

    def clear_connected(self, addr) -> None:
        with self._lock:
            if self.client_addr == f"{addr[0]}:{addr[1]}":
                self.connected = False
                self.client_addr = ""

    def set_hello(self, seq: int, summary: str, board_name: str) -> None:
        with self._lock:
            self.last_hello = f"seq={seq} {summary}"
            if board_name:
                self.board_name = board_name

    def set_ack(self, seq: int, summary: str) -> None:
        with self._lock:
            self.last_ack_seq = seq
            self.last_ack = f"seq={seq} {summary}"

    def set_tlm(self, seq: int, summary: str, data) -> None:
        with self._lock:
            self.last_tlm_seq = seq
            self.last_tlm = f"seq={seq} {summary}"
            self.last_tlm_data = data

    def set_hold(self, active: bool, throttle: float, steer: float, period_ms: int) -> None:
        with self._lock:
            self.hold_active = bool(active)
            self.hold_throttle = throttle
            self.hold_steer = steer
            self.hold_period_ms = period_ms

    def snapshot(self) -> dict:
        with self._lock:
            tlm_data = self.last_tlm_data
            if isinstance(tlm_data, dict):
                tlm_data = dict(tlm_data)
            return {
                "client_addr": self.client_addr,
                "connected": self.connected,
                "board_name": self.board_name,
                "last_hello": self.last_hello,
                "last_ack": self.last_ack,
                "last_ack_seq": self.last_ack_seq,
                "last_tlm": self.last_tlm,
                "last_tlm_seq": self.last_tlm_seq,
                "last_tlm_data": tlm_data,
                "hold_active": self.hold_active,
                "hold_throttle": self.hold_throttle,
                "hold_steer": self.hold_steer,
                "hold_period_ms": self.hold_period_ms,
            }


class SessionHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None

    def set(self, session) -> None:
        with self._lock:
            self._session = session

    def clear(self, session) -> None:
        with self._lock:
            if self._session is session:
                self._session = None

    def get(self):
        with self._lock:
            return self._session


class ConnectionSession:
    def __init__(self, conn: socket.socket, addr, output: LogController) -> None:
        self.conn = conn
        self.addr = addr
        self.output = output
        self.lock = threading.Lock()
        self.next_seq = 1
        self.heartbeat_count = 1
        self.alive = True

    def close(self) -> None:
        with self.lock:
            self.alive = False

    def send_message(self, msg_type: int, payload: bytes, label: str, quiet: bool = False) -> int:
        with self.lock:
            if not self.alive:
                raise OSError("session is closed")
            seq = self.next_seq
            self.next_seq += 1
            self.conn.sendall(build_frame(msg_type, seq, payload))
        if not quiet:
            self.output.emit("tx", label.format(seq=seq))
        return seq

    def send_heartbeat(self) -> int:
        count = self.heartbeat_count
        self.heartbeat_count += 1
        return self.send_message(
            MSG_HEARTBEAT,
            build_heartbeat_payload(count),
            "[tx]    heartbeat seq={seq}",
        )

    def send_set_input(self, throttle: float, steer: float, quiet: bool = False) -> int:
        payload = build_set_input_payload(throttle, steer, INPUT_SOURCES["wifi"])
        return self.send_message(
            MSG_SET_INPUT,
            payload,
            f"[tx]    set_input thr={throttle:+.2f} steer={steer:+.2f} seq={{seq}}",
            quiet=quiet,
        )

    def send_tuning(self, field: str, value: float, target_mask: int) -> int:
        payload = build_set_tuning_payload(field, value, target_mask)
        return self.send_message(
            MSG_SET_TUNING,
            payload,
            f"[tx]    set_tuning field={field} value={value:.3f} target=0x{target_mask:02X} seq={{seq}}",
        )

    def send_enable(self, enable_mask: int) -> int:
        payload = build_set_enable_payload(enable_mask)
        return self.send_message(
            MSG_SET_ENABLE,
            payload,
            f"[tx]    set_enable mask=0x{enable_mask:02X} seq={{seq}}",
        )

    def send_mode(self, ctrl_mode: int, selection: int, input_src: int) -> int:
        payload = build_set_mode_payload(ctrl_mode, selection, input_src)
        return self.send_message(
            MSG_SET_MODE,
            payload,
            f"[tx]    set_mode mode={ctrl_mode} sel={selection} src={input_src} seq={{seq}}",
        )


class InputStreamer:
    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active = False
        self.throttle = 0.0
        self.steer = 0.0
        self.period_s = 0.05
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def configure(self, throttle: float, steer: float, period_ms: int) -> None:
        with self.lock:
            self.throttle = throttle
            self.steer = steer
            self.period_s = max(period_ms, 20) / 1000.0
            self.active = True

    def disable(self) -> None:
        with self.lock:
            self.active = False

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                active = self.active
                throttle = self.throttle
                steer = self.steer
                period_s = self.period_s

            if not active:
                if self.stop_event.wait(0.1):
                    break
                continue

            session = self.hub.get()
            if session is not None:
                try:
                    session.send_set_input(throttle, steer, quiet=True)
                except OSError:
                    pass

            if self.stop_event.wait(period_s):
                break


class ServerContext:
    def __init__(self, output: LogController = None, state: RuntimeState = None, stop_event: threading.Event = None) -> None:
        self.output = output if output is not None else LogController()
        self.state = state if state is not None else RuntimeState()
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.hub = SessionHub()
        self.input_streamer = InputStreamer(self.hub)
        self._closed = False
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.stop_event.set()
        self.input_streamer.close()


def discovery_thread(stop_event: threading.Event, tcp_port: int, udp_port: int, output: LogController) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", udp_port))
    sock.settimeout(0.5)

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(256)
        except socket.timeout:
            continue
        except OSError:
            break

        if data.strip() == b"LED2_DISCOVER_V1":
            reply = f"LED2_SERVER_V1 {tcp_port}".encode("ascii")
            sock.sendto(reply, addr)
            output.emit("udp", f"[udp] replied to discovery from {addr[0]}:{addr[1]}")

    sock.close()


def emit_help(output: LogController) -> None:
    lines = [
        "Commands:",
        "  help",
        "  status",
        "  hb",
        "  input <throttle> <steer>",
        "  hold <throttle> <steer> [period_ms]",
        "  hold off",
        "  tune <kp|kw|tff|speed|limit|ramp|tlimit> <value> [m0|m1|both]",
        "  enable <off|m0|m1|both|0|1|2|3>",
        "  mode <indep|sync|diff> <m0|m1|both> <kbd|rc|wifi>",
        "  ack <on|off>",
        "  tlm <on|off>",
    ]
    for line in lines:
        output.emit("cmd", line)


def parse_enable_mask(token: str) -> int:
    key = token.lower()
    if key in ("off", "0"):
        return 0
    if key in ("on", "both", "3"):
        return 0x03
    if key in ("m0", "1"):
        return 0x01
    if key in ("m1", "2"):
        return 0x02
    raise ValueError(f"invalid enable mask: {token}")


def parse_on_off(token: str, usage_name: str) -> bool:
    key = token.lower()
    if key == "on":
        return True
    if key == "off":
        return False
    raise ValueError(f"usage: {usage_name} <on|off>")


def emit_status(ctx: ServerContext) -> None:
    snapshot = ctx.state.snapshot()
    filters = ctx.output.filters_snapshot()
    if snapshot["connected"]:
        ctx.output.emit("cmd", f"[cmd] client={snapshot['client_addr']}")
    else:
        ctx.output.emit("cmd", "[cmd] no client connected")

    ctx.output.emit(
        "cmd",
        f"[cmd] log ack={'on' if filters['ack'] else 'off'} tlm={'on' if filters['tlm'] else 'off'}",
    )
    if snapshot["hold_active"]:
        ctx.output.emit(
            "cmd",
            (
                f"[cmd] hold active thr={snapshot['hold_throttle']:+.2f} "
                f"steer={snapshot['hold_steer']:+.2f} every {snapshot['hold_period_ms']}ms"
            ),
        )
    else:
        ctx.output.emit("cmd", "[cmd] hold inactive")


def execute_command(line: str, ctx: ServerContext) -> bool:
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        ctx.output.emit("cmd", f"[cmd] parse error: {exc}")
        return False

    if not tokens:
        return True

    cmd = tokens[0].lower()
    if cmd in ("help", "?"):
        emit_help(ctx.output)
        return True

    if cmd == "status":
        emit_status(ctx)
        return True

    if cmd == "ack":
        try:
            if len(tokens) != 2:
                raise ValueError("usage: ack <on|off>")
            enabled = parse_on_off(tokens[1], "ack")
            ctx.output.set_visible("ack", enabled)
            ctx.output.emit("cmd", f"[cmd] ack log {'on' if enabled else 'off'}")
            return True
        except ValueError as exc:
            ctx.output.emit("cmd", f"[cmd] {exc}")
            return False

    if cmd == "tlm":
        try:
            if len(tokens) != 2:
                raise ValueError("usage: tlm <on|off>")
            enabled = parse_on_off(tokens[1], "tlm")
            ctx.output.set_visible("tlm", enabled)
            ctx.output.emit("cmd", f"[cmd] tlm log {'on' if enabled else 'off'}")
            return True
        except ValueError as exc:
            ctx.output.emit("cmd", f"[cmd] {exc}")
            return False

    if cmd == "hold":
        try:
            if len(tokens) == 2 and tokens[1].lower() == "off":
                ctx.input_streamer.disable()
                ctx.state.set_hold(False, 0.0, 0.0, 50)
                ctx.output.emit("cmd", "[cmd] continuous input stopped")
                return True
            if len(tokens) in (3, 4):
                throttle = float(tokens[1])
                steer = float(tokens[2])
                period_ms = 50 if len(tokens) == 3 else int(tokens[3])
                period_ms = max(period_ms, 20)
                ctx.input_streamer.configure(throttle, steer, period_ms)
                ctx.state.set_hold(True, throttle, steer, period_ms)
                ctx.output.emit(
                    "cmd",
                    f"[cmd] continuous input thr={throttle:+.2f} steer={steer:+.2f} every {period_ms}ms",
                )
                return True
            raise ValueError("usage: hold <throttle> <steer> [period_ms] | hold off")
        except ValueError as exc:
            ctx.output.emit("cmd", f"[cmd] {exc}")
            return False

    session = ctx.hub.get()
    if session is None:
        ctx.output.emit("cmd", "[cmd] no client connected")
        return False

    try:
        if cmd == "hb":
            session.send_heartbeat()
        elif cmd == "input":
            if len(tokens) != 3:
                raise ValueError("usage: input <throttle> <steer>")
            session.send_set_input(float(tokens[1]), float(tokens[2]))
        elif cmd == "tune":
            if len(tokens) not in (3, 4):
                raise ValueError("usage: tune <field> <value> [m0|m1|both]")
            field = tokens[1].lower()
            value = float(tokens[2])
            target_mask = 0x03 if len(tokens) == 3 else TARGET_MASKS[tokens[3].lower()]
            session.send_tuning(field, value, target_mask)
        elif cmd == "enable":
            if len(tokens) != 2:
                raise ValueError("usage: enable <off|m0|m1|both|0|1|2|3>")
            session.send_enable(parse_enable_mask(tokens[1]))
        elif cmd == "mode":
            if len(tokens) != 4:
                raise ValueError("usage: mode <indep|sync|diff> <m0|m1|both> <kbd|rc|wifi>")
            ctrl_mode = CTRL_MODES[tokens[1].lower()]
            selection = SELECTIONS[tokens[2].lower()]
            input_src = INPUT_SOURCES[tokens[3].lower()]
            session.send_mode(ctrl_mode, selection, input_src)
        else:
            raise ValueError(f"unknown command: {cmd}")
    except KeyError as exc:
        ctx.output.emit("cmd", f"[cmd] invalid token: {exc}")
        return False
    except ValueError as exc:
        ctx.output.emit("cmd", f"[cmd] {exc}")
        return False
    except OSError as exc:
        ctx.output.emit("cmd", f"[cmd] send failed: {exc}")
        return False

    return True


def run_console(ctx: ServerContext) -> None:
    if not sys.stdin.isatty():
        return

    emit_help(ctx.output)
    while True:
        try:
            line = input("cmd> ").strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            ctx.output.emit("cmd", "")
            return

        if not line:
            continue

        execute_command(line, ctx)


def handle_client(
    conn: socket.socket,
    addr,
    heartbeat_ms: int,
    ctx: ServerContext,
    heartbeat_before_hello: bool,
    hello_timeout_ms: int,
) -> None:
    ctx.output.emit("tcp", f"[tcp] client connected: {addr[0]}:{addr[1]}")
    ctx.state.set_connected(addr)
    buf = bytearray()
    stop_event = threading.Event()
    hello_event = threading.Event()
    heartbeat_gate_forced = threading.Event()
    connected_at = time.monotonic()
    session = ConnectionSession(conn, addr, ctx.output)
    ctx.hub.set(session)

    try:
        conn.settimeout(1.0)
    except OSError:
        pass

    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass

    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    def heartbeat_sender() -> None:
        interval = max(heartbeat_ms, 50) / 1000.0
        while not stop_event.is_set() and not ctx.stop_event.is_set():
            if not heartbeat_before_hello and not hello_event.is_set():
                if (
                    hello_timeout_ms > 0
                    and not heartbeat_gate_forced.is_set()
                    and ((time.monotonic() - connected_at) * 1000.0) >= float(hello_timeout_ms)
                ):
                    heartbeat_gate_forced.set()
                    ctx.output.emit("tcp", f"[tcp] hello timeout fallback heartbeat after {hello_timeout_ms} ms")
                if stop_event.wait(0.05):
                    break
                if not heartbeat_gate_forced.is_set():
                    continue
            try:
                session.send_heartbeat()
            except OSError as exc:
                if not stop_event.is_set():
                    ctx.output.emit("tcp", f"[tcp] heartbeat send failed: {exc}")
                stop_event.set()
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                break
            if stop_event.wait(interval):
                break

    sender = threading.Thread(target=heartbeat_sender, daemon=True)
    if heartbeat_before_hello:
        ctx.output.emit("tcp", "[tcp] heartbeat mode: immediate")
    else:
        ctx.output.emit("tcp", f"[tcp] heartbeat mode: wait hello fallback={hello_timeout_ms}ms")
    sender.start()

    try:
        while not stop_event.is_set() and not ctx.stop_event.is_set():
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                ctx.output.emit("tcp", f"[tcp] recv failed: {exc}")
                break

            if not chunk:
                break
            buf.extend(chunk)

            while True:
                if len(buf) < 8:
                    break

                sof = read_le_u16(buf, 0)
                if sof != SOF:
                    del buf[0]
                    continue

                _, ver, msg_type, seq, payload_len = struct.unpack_from("<HBBHH", buf, 0)
                frame_len = 8 + payload_len + 2
                if len(buf) < frame_len:
                    break

                frame = bytes(buf[:frame_len])
                del buf[:frame_len]

                if ver != PROTO_VER:
                    ctx.output.emit("tcp", f"[tcp] drop seq={seq}: unsupported version {ver}")
                    continue

                expect_crc = crc16_ccitt(frame[:-2])
                got_crc = read_le_u16(frame, frame_len - 2)
                if expect_crc != got_crc:
                    ctx.output.emit(
                        "tcp",
                        f"[tcp] drop seq={seq}: crc mismatch expect=0x{expect_crc:04X} got=0x{got_crc:04X}",
                    )
                    continue

                payload = frame[8:-2]
                if msg_type == MSG_HELLO:
                    if not hello_event.is_set():
                        hello_event.set()
                        if not heartbeat_before_hello and not heartbeat_gate_forced.is_set():
                            ctx.output.emit("tcp", "[tcp] heartbeat gate open")
                    hello_data = parse_hello_payload(payload)
                    summary = hello_summary(payload)
                    ctx.state.set_hello(seq, summary, hello_data["board"])
                    ctx.output.emit("hello", f"[hello] seq={seq} {summary}")
                elif msg_type == MSG_ACK:
                    summary = ack_summary(payload)
                    ctx.state.set_ack(seq, summary)
                    ctx.output.emit("ack", f"[ack]   seq={seq} {summary}")
                elif msg_type == MSG_TELEMETRY:
                    if payload_len == 44:
                        summary = bridge_telemetry_summary(payload)
                        tlm_data = None
                    else:
                        tlm_data = parse_telemetry(payload)
                        summary = telemetry_summary_from_data(tlm_data)
                    ctx.state.set_tlm(seq, summary, tlm_data)
                    ctx.output.emit("tlm", f"[tlm]   seq={seq} {summary}")
                else:
                    ctx.output.emit("tcp", f"[tcp] seq={seq} type=0x{msg_type:02X} len={payload_len}")
    finally:
        stop_event.set()
        session.close()
        ctx.hub.clear(session)
        ctx.state.clear_connected(addr)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        ctx.output.emit("tcp", f"[tcp] client disconnected: {addr[0]}:{addr[1]}")


def serve_forever(
    host: str,
    tcp_port: int,
    udp_port: int,
    heartbeat_ms: int,
    heartbeat_before_hello: bool,
    hello_timeout_ms: int,
    ctx: ServerContext = None,
    run_console_input: bool = True,
) -> None:
    ctx = ctx if ctx is not None else ServerContext()
    udp_worker = threading.Thread(
        target=discovery_thread,
        args=(ctx.stop_event, tcp_port, udp_port, ctx.output),
        daemon=True,
    )
    udp_worker.start()

    if run_console_input:
        console_worker = threading.Thread(target=run_console, args=(ctx,), daemon=True)
        console_worker.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, tcp_port))
        sock.listen(1)
        sock.settimeout(0.5)
    except OSError as exc:
        ctx.output.emit("tcp", f"[tcp] server start failed: {exc}")
        ctx.close()
        sock.close()
        return

    ctx.output.emit("tcp", f"TCP telemetry server listening on {host}:{tcp_port}")
    ctx.output.emit("udp", f"UDP discovery responder listening on 0.0.0.0:{udp_port}")

    try:
        while not ctx.stop_event.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                handle_client(conn, addr, heartbeat_ms, ctx, heartbeat_before_hello, hello_timeout_ms)
            except Exception as exc:
                ctx.output.emit("tcp", f"[tcp] session handler error: {exc}")
    except KeyboardInterrupt:
        pass
    finally:
        ctx.close()
        sock.close()


class WiFiControlPanel:
    def __init__(self, ctx: ServerContext, host: str, tcp_port: int, udp_port: int) -> None:
        self.ctx = ctx
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.log_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title("LED_2 WiFi Pilot Control")
        self.root.geometry("1220x860")
        self.root.minsize(1080, 760)

        self.server_var = tk.StringVar(value=f"TCP {host}:{tcp_port} / UDP 0.0.0.0:{udp_port}")
        self.client_var = tk.StringVar(value="Client: disconnected")
        self.filter_var = tk.StringVar(value="Logs: ack=on tlm=on")
        self.hold_var = tk.StringVar(value="Hold: inactive")
        self.hello_var = tk.StringVar(value="HELLO: waiting")
        self.ack_var = tk.StringVar(value="ACK: waiting")
        self.telemetry_var = tk.StringVar(value="Telemetry: waiting")
        self.motor_var = tk.StringVar(value="Motor: waiting")
        self.bus_var = tk.StringVar(value="Bus: waiting")

        self.mode_var = tk.StringVar(value="diff")
        self.selection_var = tk.StringVar(value="both")
        self.source_var = tk.StringVar(value="wifi")
        self.enable_var = tk.StringVar(value="both")
        self.tune_field_var = tk.StringVar(value="speed")
        self.tune_value_var = tk.StringVar(value="30")
        self.tune_target_var = tk.StringVar(value="both")
        self.period_var = tk.StringVar(value="50")
        self.throttle_var = tk.DoubleVar(value=0.00)
        self.steer_var = tk.DoubleVar(value=0.00)
        self.ack_log_var = tk.BooleanVar(value=self.ctx.output.is_visible("ack"))
        self.tlm_log_var = tk.BooleanVar(value=self.ctx.output.is_visible("tlm"))
        self.global_tune_vars = {
            "speed": tk.StringVar(value="30.0"),
            "limit": tk.StringVar(value="180.0"),
            "ramp": tk.StringVar(value="0.80"),
            "tlimit": tk.StringVar(value="0.60"),
        }
        self.motor_tune_vars = {
            "m0": {
                "kp": tk.StringVar(value="0.00"),
                "kw": tk.StringVar(value="0.10"),
                "tff": tk.StringVar(value="0.00"),
            },
            "m1": {
                "kp": tk.StringVar(value="0.00"),
                "kw": tk.StringVar(value="0.10"),
                "tff": tk.StringVar(value="0.00"),
            },
        }

        self.ctx.output.add_listener(self._on_log_event)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._refresh)

    def run(self) -> None:
        self.root.mainloop()

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=1)

        status_box = ttk.LabelFrame(top, text="Status", padding=10)
        status_box.grid(row=0, column=0, sticky="ew")
        status_box.columnconfigure(0, weight=1)
        ttk.Label(status_box, textvariable=self.server_var).grid(row=0, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.client_var).grid(row=1, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.filter_var).grid(row=2, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.hold_var).grid(row=3, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.hello_var, wraplength=1120).grid(row=4, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.ack_var, wraplength=1120).grid(row=5, column=0, sticky="w")

        control = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        control.grid(row=1, column=0, sticky="ew")
        control.columnconfigure(0, weight=1)
        control.columnconfigure(1, weight=1)

        quick_box = ttk.LabelFrame(control, text="Quick Actions", padding=10)
        quick_box.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        for column in range(4):
            quick_box.columnconfigure(column, weight=1)
        ttk.Button(quick_box, text="WiFi Takeover", command=lambda: self._run_command("mode diff both wifi")).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Enable Both", command=lambda: self._run_command("enable both")).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Disable All", command=lambda: self._run_command("enable off")).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Status", command=lambda: self._run_command("status")).grid(row=0, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Zero Hold", command=lambda: self._run_command("hold 0.00 0.00 50")).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Hold Off", command=lambda: self._run_command("hold off")).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="HB Once", command=lambda: self._run_command("hb")).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(quick_box, text="Safe Stop", command=self._safe_stop).grid(row=1, column=3, sticky="ew", padx=2, pady=2)

        command_box = ttk.LabelFrame(control, text="Command Builder", padding=10)
        command_box.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        for column in range(4):
            command_box.columnconfigure(column, weight=1)
        ttk.Label(command_box, text="Mode").grid(row=0, column=0, sticky="w")
        ttk.Label(command_box, text="Select").grid(row=0, column=1, sticky="w")
        ttk.Label(command_box, text="Source").grid(row=0, column=2, sticky="w")
        ttk.Label(command_box, text="Enable").grid(row=0, column=3, sticky="w")
        ttk.Combobox(command_box, textvariable=self.mode_var, values=("indep", "sync", "diff"), state="readonly").grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Combobox(command_box, textvariable=self.selection_var, values=("m0", "m1", "both"), state="readonly").grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Combobox(command_box, textvariable=self.source_var, values=("kbd", "rc", "wifi"), state="readonly").grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Combobox(command_box, textvariable=self.enable_var, values=("off", "m0", "m1", "both"), state="readonly").grid(row=1, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(command_box, text="Apply Mode", command=self._apply_mode).grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        ttk.Button(command_box, text="Apply Enable", command=self._apply_enable).grid(row=2, column=2, columnspan=2, sticky="ew", padx=2, pady=2)

        hold_box = ttk.LabelFrame(self.root, text="Hold Input", padding=10)
        hold_box.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        hold_box.columnconfigure(1, weight=1)
        hold_box.columnconfigure(3, weight=1)
        ttk.Label(hold_box, text="Throttle").grid(row=0, column=0, sticky="w")
        tk.Scale(
            hold_box,
            from_=-1.0,
            to=1.0,
            resolution=0.01,
            orient="horizontal",
            variable=self.throttle_var,
            length=320,
        ).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(hold_box, text="Steer").grid(row=0, column=2, sticky="w")
        tk.Scale(
            hold_box,
            from_=-1.0,
            to=1.0,
            resolution=0.01,
            orient="horizontal",
            variable=self.steer_var,
            length=320,
        ).grid(row=0, column=3, sticky="ew", padx=5)
        ttk.Label(hold_box, text="Period ms").grid(row=0, column=4, sticky="w", padx=(10, 0))
        ttk.Entry(hold_box, textvariable=self.period_var, width=8).grid(row=0, column=5, sticky="w")
        ttk.Button(hold_box, text="Start Hold", command=self._start_hold).grid(row=0, column=6, sticky="ew", padx=4)
        ttk.Button(hold_box, text="Send Once", command=self._send_once).grid(row=0, column=7, sticky="ew", padx=4)
        ttk.Button(hold_box, text="Stop Hold", command=lambda: self._run_command("hold off")).grid(row=0, column=8, sticky="ew", padx=4)

        tune_box = ttk.LabelFrame(self.root, text="Runtime Tuning", padding=10)
        tune_box.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        tune_box.columnconfigure(0, weight=1)
        tune_box.rowconfigure(3, weight=1)

        tune_controls = ttk.Frame(tune_box)
        tune_controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for column in range(6):
            tune_controls.columnconfigure(column, weight=1)
        ttk.Label(tune_controls, text="Field").grid(row=0, column=0, sticky="w")
        ttk.Label(tune_controls, text="Value").grid(row=0, column=1, sticky="w")
        ttk.Label(tune_controls, text="Target").grid(row=0, column=2, sticky="w")
        ttk.Combobox(tune_controls, textvariable=self.tune_field_var, values=("kp", "kw", "tff", "speed", "limit", "ramp", "tlimit"), state="readonly").grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Entry(tune_controls, textvariable=self.tune_value_var).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Combobox(tune_controls, textvariable=self.tune_target_var, values=("m0", "m1", "both"), state="readonly").grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(tune_controls, text="Apply Tune", command=self._apply_tune).grid(row=1, column=3, sticky="ew", padx=2, pady=2)
        ttk.Checkbutton(tune_controls, text="Show ACK", variable=self.ack_log_var, command=self._toggle_ack_log).grid(row=1, column=4, sticky="w", padx=8)
        ttk.Checkbutton(tune_controls, text="Show TLM", variable=self.tlm_log_var, command=self._toggle_tlm_log).grid(row=1, column=5, sticky="w", padx=8)

        telemetry_box = ttk.LabelFrame(tune_box, text="Live Telemetry", padding=10)
        telemetry_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        telemetry_box.columnconfigure(0, weight=1)
        ttk.Label(telemetry_box, textvariable=self.telemetry_var, wraplength=1120).grid(row=0, column=0, sticky="w")
        ttk.Label(telemetry_box, textvariable=self.motor_var, wraplength=1120).grid(row=1, column=0, sticky="w")
        ttk.Label(telemetry_box, textvariable=self.bus_var, wraplength=1120).grid(row=2, column=0, sticky="w")

        bulk_tune_box = ttk.LabelFrame(tune_box, text="Full Parameter Panel", padding=10)
        bulk_tune_box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        bulk_tune_box.columnconfigure(0, weight=1)
        bulk_tune_box.columnconfigure(1, weight=1)

        global_box = ttk.LabelFrame(bulk_tune_box, text="Global", padding=10)
        global_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        for column in range(4):
            global_box.columnconfigure(column, weight=1)
        ttk.Label(global_box, text="Speed").grid(row=0, column=0, sticky="w")
        ttk.Label(global_box, text="Limit").grid(row=0, column=1, sticky="w")
        ttk.Label(global_box, text="Ramp").grid(row=0, column=2, sticky="w")
        ttk.Label(global_box, text="TLimit").grid(row=0, column=3, sticky="w")
        ttk.Entry(global_box, textvariable=self.global_tune_vars["speed"]).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Entry(global_box, textvariable=self.global_tune_vars["limit"]).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Entry(global_box, textvariable=self.global_tune_vars["ramp"]).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Entry(global_box, textvariable=self.global_tune_vars["tlimit"]).grid(row=1, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(global_box, text="Load Current", command=self._load_tuning_from_telemetry).grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=4)
        ttk.Button(global_box, text="Apply Global", command=self._apply_global_tuning).grid(row=2, column=2, columnspan=2, sticky="ew", padx=2, pady=4)

        motor_box = ttk.LabelFrame(bulk_tune_box, text="Per Motor", padding=10)
        motor_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for column in range(4):
            motor_box.columnconfigure(column, weight=1)
        ttk.Label(motor_box, text="Field").grid(row=0, column=0, sticky="w")
        ttk.Label(motor_box, text="M0").grid(row=0, column=1, sticky="w")
        ttk.Label(motor_box, text="M1").grid(row=0, column=2, sticky="w")
        ttk.Label(motor_box, text="Action").grid(row=0, column=3, sticky="w")
        for row, field in enumerate(("kp", "kw", "tff"), start=1):
            ttk.Label(motor_box, text=field.upper()).grid(row=row, column=0, sticky="w")
            ttk.Entry(motor_box, textvariable=self.motor_tune_vars["m0"][field]).grid(row=row, column=1, sticky="ew", padx=2, pady=2)
            ttk.Entry(motor_box, textvariable=self.motor_tune_vars["m1"][field]).grid(row=row, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(motor_box, text="Apply M0", command=lambda: self._apply_motor_tuning("m0")).grid(row=1, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(motor_box, text="Apply M1", command=lambda: self._apply_motor_tuning("m1")).grid(row=2, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(motor_box, text="Apply All", command=self._apply_all_tuning).grid(row=3, column=3, sticky="ew", padx=2, pady=2)

        log_box = ttk.LabelFrame(tune_box, text="Log", padding=10)
        log_box.grid(row=3, column=0, sticky="nsew")
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_box, wrap="word", height=18, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        ttk.Button(log_box, text="Clear Log", command=self._clear_log).grid(row=1, column=0, sticky="e", pady=(6, 0))

    def _on_log_event(self, category: str, line: str, visible: bool) -> None:
        if visible:
            self.log_queue.put(f"{line}\n")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _run_command(self, line: str) -> None:
        execute_command(line, self.ctx)

    def _apply_mode(self) -> None:
        self._run_command(f"mode {self.mode_var.get()} {self.selection_var.get()} {self.source_var.get()}")

    def _apply_enable(self) -> None:
        self._run_command(f"enable {self.enable_var.get()}")

    def _apply_tune(self) -> None:
        self._run_command(f"tune {self.tune_field_var.get()} {self.tune_value_var.get()} {self.tune_target_var.get()}")

    def _parse_tune_value(self, var: tk.StringVar, label: str):
        raw = var.get().strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            self.ctx.output.emit("cmd", f"[cmd] invalid {label}: {raw}")
            return None

    def _send_tune_value(self, field: str, value: float, target: str) -> None:
        self._run_command(f"tune {field} {value:.3f} {target}")

    def _load_tuning_from_telemetry(self) -> None:
        snapshot = self.ctx.state.snapshot()
        tlm_data = snapshot.get("last_tlm_data")
        if not isinstance(tlm_data, dict):
            self.ctx.output.emit("cmd", "[cmd] no telemetry available to load")
            return

        self.global_tune_vars["speed"].set(f"{tlm_data['speed_step']:.3f}")
        self.global_tune_vars["limit"].set(f"{tlm_data['w_limit']:.3f}")
        self.global_tune_vars["ramp"].set(f"{tlm_data['ramp_step']:.3f}")
        self.global_tune_vars["tlimit"].set(f"{tlm_data['t_limit']:.3f}")

        for motor_name, cmd in (("m0", tlm_data["cmds"][0]), ("m1", tlm_data["cmds"][1])):
            self.motor_tune_vars[motor_name]["kp"].set(f"{cmd[1]:.3f}")
            self.motor_tune_vars[motor_name]["kw"].set(f"{cmd[2]:.3f}")
            self.motor_tune_vars[motor_name]["tff"].set(f"{cmd[5]:.3f}")

        self.ctx.output.emit("cmd", "[cmd] tuning values loaded from telemetry")

    def _apply_global_tuning(self) -> None:
        for field in ("speed", "limit", "ramp", "tlimit"):
            value = self._parse_tune_value(self.global_tune_vars[field], field)
            if value is None:
                continue
            self._send_tune_value(field, value, "both")

    def _apply_motor_tuning(self, motor_name: str) -> None:
        for field in ("kp", "kw", "tff"):
            value = self._parse_tune_value(self.motor_tune_vars[motor_name][field], f"{motor_name}.{field}")
            if value is None:
                continue
            self._send_tune_value(field, value, motor_name)

    def _apply_all_tuning(self) -> None:
        self._apply_global_tuning()
        self._apply_motor_tuning("m0")
        self._apply_motor_tuning("m1")

    def _start_hold(self) -> None:
        self._run_command(
            f"hold {self.throttle_var.get():.2f} {self.steer_var.get():.2f} {self._safe_period_ms()}"
        )

    def _send_once(self) -> None:
        self._run_command(f"input {self.throttle_var.get():.2f} {self.steer_var.get():.2f}")

    def _safe_stop(self) -> None:
        self._run_command("hold off")
        self._run_command("enable off")

    def _toggle_ack_log(self) -> None:
        self.ctx.output.set_visible("ack", self.ack_log_var.get())
        self.ctx.output.emit("cmd", f"[cmd] ack log {'on' if self.ack_log_var.get() else 'off'}")

    def _toggle_tlm_log(self) -> None:
        self.ctx.output.set_visible("tlm", self.tlm_log_var.get())
        self.ctx.output.emit("cmd", f"[cmd] tlm log {'on' if self.tlm_log_var.get() else 'off'}")

    def _safe_period_ms(self) -> int:
        try:
            return max(int(self.period_var.get()), 20)
        except ValueError:
            return 50

    def _refresh(self) -> None:
        while True:
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break

        snapshot = self.ctx.state.snapshot()
        filters = self.ctx.output.filters_snapshot()

        if snapshot["connected"]:
            self.client_var.set(f"Client: {snapshot['client_addr']}")
        else:
            self.client_var.set("Client: disconnected")

        self.filter_var.set(
            f"Logs: ack={'on' if filters['ack'] else 'off'} tlm={'on' if filters['tlm'] else 'off'}"
        )

        if snapshot["hold_active"]:
            self.hold_var.set(
                (
                    f"Hold: active thr={snapshot['hold_throttle']:+.2f} "
                    f"steer={snapshot['hold_steer']:+.2f} every {snapshot['hold_period_ms']}ms"
                )
            )
        else:
            self.hold_var.set("Hold: inactive")

        self.hello_var.set("HELLO: " + (snapshot["last_hello"] if snapshot["last_hello"] else "waiting"))
        self.ack_var.set("ACK: " + (snapshot["last_ack"] if snapshot["last_ack"] else "waiting"))

        tlm_data = snapshot["last_tlm_data"]
        if isinstance(tlm_data, dict):
            fbk0 = tlm_data["fbks"][0]
            fbk1 = tlm_data["fbks"][1]
            self.telemetry_var.set(
                (
                    "Telemetry: "
                    f"seq={snapshot['last_tlm_seq']} src={INPUT_SOURCE_NAMES.get(tlm_data['input_src'], tlm_data['input_src'])} "
                    f"mode={CTRL_MODE_NAMES.get(tlm_data['ctrl_mode'], tlm_data['ctrl_mode'])} "
                    f"sel={SELECTION_NAMES.get(tlm_data['sel'], tlm_data['sel'])} "
                    f"en=0x{tlm_data['en_mask']:02X} "
                    f"thr={tlm_data['in_throttle']:+.2f} steer={tlm_data['in_steer']:+.2f} "
                    f"speed={tlm_data['speed_step']:.1f}/{tlm_data['w_limit']:.1f} ramp={tlm_data['ramp_step']:.2f}"
                )
            )
            self.motor_var.set(
                (
                    f"Motor: m0 w={fbk0[6]:+.2f} t={fbk0[7]:+.2f} temp={fbk0[3]} "
                    f"| m1 w={fbk1[6]:+.2f} t={fbk1[7]:+.2f} temp={fbk1[3]}"
                )
            )
            self.bus_var.set(
                (
                    "Bus: "
                    f"rs485 ok={tlm_data['rs485_rx_ok_cnt']} crc={tlm_data['rs485_rx_crc_err_cnt']} "
                    f"txe={tlm_data['rs485_tx_err_cnt']} recover={tlm_data['recover_req']}/{tlm_data['recover_run']}"
                )
            )
        else:
            self.telemetry_var.set("Telemetry: " + (snapshot["last_tlm"] if snapshot["last_tlm"] else "waiting"))
            self.motor_var.set("Motor: waiting")
            self.bus_var.set("Bus: waiting")

        self.ack_log_var.set(filters["ack"])
        self.tlm_log_var.set(filters["tlm"])
        self.root.after(150, self._refresh)

    def _on_close(self) -> None:
        self.ctx.close()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="LED_2 WiFi telemetry and command server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--tcp-port", type=int, default=TCP_PORT)
    parser.add_argument("--udp-port", type=int, default=UDP_PORT)
    parser.add_argument("--heartbeat-ms", type=int, default=1000)
    parser.add_argument(
        "--heartbeat-before-hello",
        action="store_true",
        help="send heartbeat immediately after TCP connect instead of waiting for HELLO",
    )
    parser.add_argument(
        "--hello-timeout-ms",
        type=int,
        default=1500,
        help="when waiting for HELLO, start heartbeat fallback after this timeout; 0 disables fallback",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="open a Tkinter control panel instead of the stdin command console",
    )
    args = parser.parse_args()

    if args.ui:
        if tk is None or ttk is None or scrolledtext is None:
            raise SystemExit("tkinter is not available in this Python runtime")

        ctx = ServerContext(output=LogController(console_enabled=False))
        panel = WiFiControlPanel(ctx, args.host, args.tcp_port, args.udp_port)
        server_thread = threading.Thread(
            target=serve_forever,
            args=(
                args.host,
                args.tcp_port,
                args.udp_port,
                args.heartbeat_ms,
                args.heartbeat_before_hello,
                args.hello_timeout_ms,
            ),
            kwargs={
                "ctx": ctx,
                "run_console_input": False,
            },
            daemon=True,
        )
        server_thread.start()
        panel.run()
        server_thread.join(timeout=1.0)
        return

    serve_forever(
        args.host,
        args.tcp_port,
        args.udp_port,
        args.heartbeat_ms,
        args.heartbeat_before_hello,
        args.hello_timeout_ms,
    )


if __name__ == "__main__":
    main()
