#!/usr/bin/env python3
import argparse
import shlex
import socket
import struct
import sys
import threading
import time


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


def telemetry_summary(payload: bytes) -> str:
    data = parse_telemetry(payload)
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
    tick_ms = read_le_u32(payload, 0)
    backend = payload[4]
    link_state = payload[5]
    tcp_connected = payload[6]
    board = payload[8:24].split(b"\0", 1)[0].decode("ascii", errors="replace")
    telemetry_ms = read_le_u16(payload, 24)
    server_port = read_le_u16(payload, 26)
    server_ip = decode_ip(payload[28:32])
    return (
        f"tick={tick_ms}ms board={board} backend={backend} link={link_state} "
        f"tcp={tcp_connected} telemetry={telemetry_ms}ms target={server_ip}:{server_port}"
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
    def __init__(self, conn: socket.socket, addr) -> None:
        self.conn = conn
        self.addr = addr
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
            print(label.format(seq=seq))
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


def discovery_thread(stop_event: threading.Event, tcp_port: int, udp_port: int) -> None:
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
            print(f"[udp] replied to discovery from {addr[0]}:{addr[1]}")

    sock.close()


def print_help() -> None:
    print("Commands:")
    print("  help")
    print("  status")
    print("  hb")
    print("  input <throttle> <steer>")
    print("  hold <throttle> <steer> [period_ms]")
    print("  hold off")
    print("  tune <kp|kw|tff|speed|limit|ramp|tlimit> <value> [m0|m1|both]")
    print("  enable <off|m0|m1|both|0|1|2|3>")
    print("  mode <indep|sync|diff> <m0|m1|both> <kbd|rc|wifi>")


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


def run_console(hub: SessionHub, input_streamer: InputStreamer) -> None:
    if not sys.stdin.isatty():
        return

    print_help()
    while True:
        try:
            line = input("cmd> ").strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            print()
            return

        if not line:
            continue

        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"[cmd] parse error: {exc}")
            continue

        cmd = tokens[0].lower()
        if cmd in ("help", "?"):
            print_help()
            continue

        if cmd == "status":
            session = hub.get()
            if session is None:
                print("[cmd] no client connected")
            else:
                print(f"[cmd] client={session.addr[0]}:{session.addr[1]}")
            continue

        if cmd == "hold":
            try:
                if len(tokens) == 2 and tokens[1].lower() == "off":
                    input_streamer.disable()
                    print("[cmd] continuous input stopped")
                elif len(tokens) in (3, 4):
                    throttle = float(tokens[1])
                    steer = float(tokens[2])
                    period_ms = 50 if len(tokens) == 3 else int(tokens[3])
                    input_streamer.configure(throttle, steer, period_ms)
                    print(f"[cmd] continuous input thr={throttle:+.2f} steer={steer:+.2f} every {max(period_ms, 20)}ms")
                else:
                    raise ValueError("usage: hold <throttle> <steer> [period_ms] | hold off")
            except ValueError as exc:
                print(f"[cmd] {exc}")
            continue

        session = hub.get()
        if session is None:
            print("[cmd] no client connected")
            continue

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
            print(f"[cmd] invalid token: {exc}")
        except ValueError as exc:
            print(f"[cmd] {exc}")
        except OSError as exc:
            print(f"[cmd] send failed: {exc}")


def handle_client(
    conn: socket.socket,
    addr,
    heartbeat_ms: int,
    hub: SessionHub,
    heartbeat_before_hello: bool,
) -> None:
    print(f"[tcp] client connected: {addr[0]}:{addr[1]}")
    buf = bytearray()
    stop_event = threading.Event()
    hello_event = threading.Event()
    session = ConnectionSession(conn, addr)
    hub.set(session)

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
        while not stop_event.is_set():
            if not heartbeat_before_hello and not hello_event.is_set():
                if stop_event.wait(0.05):
                    break
                continue
            try:
                session.send_heartbeat()
            except OSError as exc:
                if not stop_event.is_set():
                    print(f"[tcp] heartbeat send failed: {exc}")
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
        print("[tcp] heartbeat mode: immediate")
    else:
        print("[tcp] heartbeat mode: wait hello")
    sender.start()

    try:
        while not stop_event.is_set():
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                print(f"[tcp] recv failed: {exc}")
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
                    print(f"[tcp] drop seq={seq}: unsupported version {ver}")
                    continue

                expect_crc = crc16_ccitt(frame[:-2])
                got_crc = read_le_u16(frame, frame_len - 2)
                if expect_crc != got_crc:
                    print(f"[tcp] drop seq={seq}: crc mismatch expect=0x{expect_crc:04X} got=0x{got_crc:04X}")
                    continue

                payload = frame[8:-2]
                if msg_type == MSG_HELLO:
                    if not hello_event.is_set():
                        hello_event.set()
                        if not heartbeat_before_hello:
                            print("[tcp] heartbeat gate open")
                    print(f"[hello] seq={seq} {hello_summary(payload)}")
                elif msg_type == MSG_ACK:
                    print(f"[ack]   seq={seq} {ack_summary(payload)}")
                elif msg_type == MSG_TELEMETRY:
                    if payload_len == 44:
                        print(f"[tlm]   seq={seq} {bridge_telemetry_summary(payload)}")
                    else:
                        print(f"[tlm]   seq={seq} {telemetry_summary(payload)}")
                else:
                    print(f"[tcp] seq={seq} type=0x{msg_type:02X} len={payload_len}")
    finally:
        stop_event.set()
        session.close()
        hub.clear(session)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        print(f"[tcp] client disconnected: {addr[0]}:{addr[1]}")


def serve_forever(
    host: str,
    tcp_port: int,
    udp_port: int,
    heartbeat_ms: int,
    heartbeat_before_hello: bool,
) -> None:
    stop_event = threading.Event()
    hub = SessionHub()
    input_streamer = InputStreamer(hub)
    udp_worker = threading.Thread(
        target=discovery_thread,
        args=(stop_event, tcp_port, udp_port),
        daemon=True,
    )
    udp_worker.start()

    console_worker = threading.Thread(target=run_console, args=(hub, input_streamer), daemon=True)
    console_worker.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, tcp_port))
    sock.listen(1)
    print(f"TCP telemetry server listening on {host}:{tcp_port}")
    print(f"UDP discovery responder listening on 0.0.0.0:{udp_port}")

    try:
        while True:
            conn, addr = sock.accept()
            try:
                handle_client(conn, addr, heartbeat_ms, hub, heartbeat_before_hello)
            except Exception as exc:
                print(f"[tcp] session handler error: {exc}")
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        input_streamer.close()
        sock.close()


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
    args = parser.parse_args()

    serve_forever(
        args.host,
        args.tcp_port,
        args.udp_port,
        args.heartbeat_ms,
        args.heartbeat_before_hello,
    )


if __name__ == "__main__":
    main()
