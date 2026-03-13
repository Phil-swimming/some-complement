#include "client.h"

#include "lwip/api.h"
#include "lwip/inet.h"
#include "lwip/ip4_addr.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"
#include "wifi_base_config.h"

#include <string.h>

#define PRINTF                           printf

#define LED2_FRAME_SOF                   (0x55AAu)
#define LED2_PROTO_VER                   (1u)
#define LED2_MSG_HELLO                   (0x01u)
#define LED2_MSG_HEARTBEAT               (0x02u)
#define LED2_MSG_ACK                     (0x80u)
#define LED2_MSG_TELEMETRY               (0x90u)

#define LED2_BACKEND_AP6181              (2u)
#define LED2_LINK_TCP_CONNECTED          (2u)

#define LED2_HELLO_PAYLOAD_LEN           (32u)
#define LED2_TELEMETRY_PAYLOAD_LEN       (44u)
#define LED2_ACK_PAYLOAD_LEN             (12u)
#define LED2_RX_BUF_LEN                  (256u)
#define LED2_TX_BUF_LEN                  (128u)
#define LED2_BOARD_NAME                  "LED2-AP6181"
#define LED2_TLM_PERIOD_MS               (20u)
#define LED2_RECONNECT_DELAY_MS          (1000u)
#define LED2_SELECT_TIMEOUT_US           (20000u)

typedef struct {
  uint8_t hello_sent;
  uint8_t local_ip[4];
  uint8_t last_rx_type;
  uint8_t reserved0;
  uint16_t next_seq;
  uint16_t last_rx_seq;
  uint16_t last_tx_len;
  uint16_t rx_len;
  uint32_t tx_frame_cnt;
  uint32_t tx_err_cnt;
  uint32_t rx_frame_cnt;
  uint32_t rx_crc_err_cnt;
  uint32_t heartbeat_rx_cnt;
  uint8_t rx_buf[LED2_RX_BUF_LEN];
} Led2BridgeState_t;

static Led2BridgeState_t s_bridge = {0};

static uint8_t led2_has_elapsed(uint32_t now, uint32_t last, uint32_t period_ms)
{
  return ((uint32_t)(now - last) >= period_ms) ? 1u : 0u;
}

static uint16_t led2_crc16_ccitt(const uint8_t *data, uint16_t len)
{
  uint16_t crc = 0xFFFFu;
  uint16_t i;
  uint8_t j;

  if (data == NULL) {
    return 0u;
  }

  for (i = 0u; i < len; ++i) {
    crc ^= (uint16_t)((uint16_t)data[i] << 8);
    for (j = 0u; j < 8u; ++j) {
      if ((crc & 0x8000u) != 0u) {
        crc = (uint16_t)((crc << 1) ^ 0x1021u);
      } else {
        crc <<= 1;
      }
    }
  }

  return crc;
}

static void led2_put_u8(uint8_t *buf, uint16_t *offset, uint8_t value)
{
  buf[*offset] = value;
  *offset = (uint16_t)(*offset + 1u);
}

static void led2_put_u16le(uint8_t *buf, uint16_t *offset, uint16_t value)
{
  led2_put_u8(buf, offset, (uint8_t)(value & 0xFFu));
  led2_put_u8(buf, offset, (uint8_t)((value >> 8) & 0xFFu));
}

static void led2_put_u32le(uint8_t *buf, uint16_t *offset, uint32_t value)
{
  led2_put_u8(buf, offset, (uint8_t)(value & 0xFFu));
  led2_put_u8(buf, offset, (uint8_t)((value >> 8) & 0xFFu));
  led2_put_u8(buf, offset, (uint8_t)((value >> 16) & 0xFFu));
  led2_put_u8(buf, offset, (uint8_t)((value >> 24) & 0xFFu));
}

static uint16_t led2_get_u16le(const uint8_t *buf)
{
  return (uint16_t)((uint16_t)buf[0] | ((uint16_t)buf[1] << 8));
}

static void led2_put_mem(uint8_t *buf, uint16_t *offset, const uint8_t *src, uint16_t len)
{
  memcpy(&buf[*offset], src, len);
  *offset = (uint16_t)(*offset + len);
}

static void led2_put_fixed_string(uint8_t *buf, uint16_t *offset, const char *text, uint16_t width)
{
  uint16_t i;
  uint8_t ch;

  for (i = 0u; i < width; ++i) {
    ch = 0u;
    if ((text != NULL) && (text[i] != '\0')) {
      ch = (uint8_t)text[i];
    }
    led2_put_u8(buf, offset, ch);
  }
}

static void led2_update_local_ip(void)
{
  uint32_t ip;

  if (ip4_addr_isany_val(*netif_ip4_addr(&g_wiced_if))) {
    memset(s_bridge.local_ip, 0, sizeof(s_bridge.local_ip));
    return;
  }

  ip = lwip_ntohl(ip4_addr_get_u32(netif_ip4_addr(&g_wiced_if)));
  s_bridge.local_ip[0] = (uint8_t)((ip >> 24) & 0xFFu);
  s_bridge.local_ip[1] = (uint8_t)((ip >> 16) & 0xFFu);
  s_bridge.local_ip[2] = (uint8_t)((ip >> 8) & 0xFFu);
  s_bridge.local_ip[3] = (uint8_t)(ip & 0xFFu);
}

static uint16_t led2_build_hello_frame(uint8_t *buf, uint16_t buf_len, uint16_t seq)
{
  uint16_t offset = 0u;
  uint16_t crc;

  if ((buf == NULL) || (buf_len < (uint16_t)(8u + LED2_HELLO_PAYLOAD_LEN + 2u))) {
    return 0u;
  }

  led2_update_local_ip();

  led2_put_u16le(buf, &offset, LED2_FRAME_SOF);
  led2_put_u8(buf, &offset, LED2_PROTO_VER);
  led2_put_u8(buf, &offset, LED2_MSG_HELLO);
  led2_put_u16le(buf, &offset, seq);
  led2_put_u16le(buf, &offset, LED2_HELLO_PAYLOAD_LEN);

  led2_put_u32le(buf, &offset, HAL_GetTick());
  led2_put_u8(buf, &offset, LED2_BACKEND_AP6181);
  led2_put_u8(buf, &offset, LED2_LINK_TCP_CONNECTED);
  led2_put_u8(buf, &offset, 1u);
  led2_put_u8(buf, &offset, 0u);
  led2_put_fixed_string(buf, &offset, LED2_BOARD_NAME, 16u);
  led2_put_u16le(buf, &offset, LED2_TLM_PERIOD_MS);
  led2_put_u16le(buf, &offset, DEST_PORT);
  led2_put_u8(buf, &offset, DEST_IP_ADDR0);
  led2_put_u8(buf, &offset, DEST_IP_ADDR1);
  led2_put_u8(buf, &offset, DEST_IP_ADDR2);
  led2_put_u8(buf, &offset, DEST_IP_ADDR3);

  crc = led2_crc16_ccitt(buf, offset);
  led2_put_u16le(buf, &offset, crc);

  return offset;
}

static uint16_t led2_build_telemetry_frame(uint8_t *buf, uint16_t buf_len, uint16_t seq)
{
  uint16_t offset = 0u;
  uint16_t crc;

  if ((buf == NULL) || (buf_len < (uint16_t)(8u + LED2_TELEMETRY_PAYLOAD_LEN + 2u))) {
    return 0u;
  }

  led2_update_local_ip();

  led2_put_u16le(buf, &offset, LED2_FRAME_SOF);
  led2_put_u8(buf, &offset, LED2_PROTO_VER);
  led2_put_u8(buf, &offset, LED2_MSG_TELEMETRY);
  led2_put_u16le(buf, &offset, seq);
  led2_put_u16le(buf, &offset, LED2_TELEMETRY_PAYLOAD_LEN);

  led2_put_u32le(buf, &offset, HAL_GetTick());
  led2_put_u8(buf, &offset, LED2_BACKEND_AP6181);
  led2_put_u8(buf, &offset, LED2_LINK_TCP_CONNECTED);
  led2_put_u8(buf, &offset, 1u);
  led2_put_u8(buf, &offset, s_bridge.hello_sent);
  led2_put_mem(buf, &offset, s_bridge.local_ip, 4u);
  led2_put_u8(buf, &offset, DEST_IP_ADDR0);
  led2_put_u8(buf, &offset, DEST_IP_ADDR1);
  led2_put_u8(buf, &offset, DEST_IP_ADDR2);
  led2_put_u8(buf, &offset, DEST_IP_ADDR3);
  led2_put_u16le(buf, &offset, DEST_PORT);
  led2_put_u16le(buf, &offset, s_bridge.last_tx_len);
  led2_put_u32le(buf, &offset, s_bridge.tx_frame_cnt);
  led2_put_u32le(buf, &offset, s_bridge.tx_err_cnt);
  led2_put_u32le(buf, &offset, s_bridge.rx_frame_cnt);
  led2_put_u32le(buf, &offset, s_bridge.rx_crc_err_cnt);
  led2_put_u32le(buf, &offset, s_bridge.heartbeat_rx_cnt);
  led2_put_u16le(buf, &offset, s_bridge.last_rx_seq);
  led2_put_u8(buf, &offset, s_bridge.last_rx_type);
  led2_put_u8(buf, &offset, 0u);

  crc = led2_crc16_ccitt(buf, offset);
  led2_put_u16le(buf, &offset, crc);

  return offset;
}

static uint16_t led2_build_ack_frame(uint8_t *buf, uint16_t buf_len, uint16_t seq,
                                     uint16_t ack_seq, uint8_t ack_type, uint8_t status)
{
  uint16_t offset = 0u;
  uint16_t crc;

  if ((buf == NULL) || (buf_len < (uint16_t)(8u + LED2_ACK_PAYLOAD_LEN + 2u))) {
    return 0u;
  }

  led2_put_u16le(buf, &offset, LED2_FRAME_SOF);
  led2_put_u8(buf, &offset, LED2_PROTO_VER);
  led2_put_u8(buf, &offset, LED2_MSG_ACK);
  led2_put_u16le(buf, &offset, seq);
  led2_put_u16le(buf, &offset, LED2_ACK_PAYLOAD_LEN);

  led2_put_u16le(buf, &offset, ack_seq);
  led2_put_u8(buf, &offset, ack_type);
  led2_put_u8(buf, &offset, status);
  led2_put_u32le(buf, &offset, s_bridge.rx_frame_cnt);
  led2_put_u32le(buf, &offset, s_bridge.heartbeat_rx_cnt);

  crc = led2_crc16_ccitt(buf, offset);
  led2_put_u16le(buf, &offset, crc);

  return offset;
}

static int led2_send_frame(int sock, const uint8_t *data, uint16_t len)
{
  uint16_t sent = 0u;
  int chunk;

  while (sent < len) {
    chunk = send(sock, data + sent, len - sent, 0);
    if (chunk <= 0) {
      s_bridge.tx_err_cnt++;
      return -1;
    }
    sent = (uint16_t)(sent + (uint16_t)chunk);
  }

  s_bridge.last_tx_len = len;
  s_bridge.tx_frame_cnt++;
  return 0;
}

static void led2_send_ack(int sock, uint16_t ack_seq, uint8_t ack_type, uint8_t status)
{
  uint8_t tx_buf[LED2_TX_BUF_LEN];
  uint16_t frame_len;

  frame_len = led2_build_ack_frame(tx_buf, sizeof(tx_buf), s_bridge.next_seq++, ack_seq, ack_type, status);
  if (frame_len == 0u) {
    s_bridge.tx_err_cnt++;
    return;
  }

  (void)led2_send_frame(sock, tx_buf, frame_len);
}

static void led2_handle_frame(int sock, uint8_t msg_type, uint16_t seq, const uint8_t *payload, uint16_t payload_len)
{
  (void)payload;

  s_bridge.rx_frame_cnt++;
  s_bridge.last_rx_type = msg_type;
  s_bridge.last_rx_seq = seq;

  if ((msg_type == LED2_MSG_HEARTBEAT) && (payload_len >= 8u)) {
    s_bridge.heartbeat_rx_cnt++;
    led2_send_ack(sock, seq, msg_type, 0u);
    return;
  }

  led2_send_ack(sock, seq, msg_type, 1u);
}

static int led2_process_rx_buffer(int sock)
{
  uint16_t payload_len;
  uint16_t frame_len;
  uint16_t seq;
  uint16_t expect_crc;
  uint16_t got_crc;

  while (s_bridge.rx_len >= 10u) {
    if (led2_get_u16le(s_bridge.rx_buf) != LED2_FRAME_SOF) {
      memmove(s_bridge.rx_buf, s_bridge.rx_buf + 1, s_bridge.rx_len - 1u);
      s_bridge.rx_len = (uint16_t)(s_bridge.rx_len - 1u);
      continue;
    }

    payload_len = led2_get_u16le(&s_bridge.rx_buf[6]);
    frame_len = (uint16_t)(8u + payload_len + 2u);
    if (frame_len > LED2_RX_BUF_LEN) {
      s_bridge.rx_len = 0u;
      s_bridge.rx_crc_err_cnt++;
      return -1;
    }

    if (s_bridge.rx_len < frame_len) {
      return 0;
    }

    expect_crc = led2_crc16_ccitt(s_bridge.rx_buf, (uint16_t)(frame_len - 2u));
    got_crc = led2_get_u16le(&s_bridge.rx_buf[frame_len - 2u]);
    if (expect_crc != got_crc) {
      s_bridge.rx_crc_err_cnt++;
      memmove(s_bridge.rx_buf, s_bridge.rx_buf + frame_len, s_bridge.rx_len - frame_len);
      s_bridge.rx_len = (uint16_t)(s_bridge.rx_len - frame_len);
      continue;
    }

    seq = led2_get_u16le(&s_bridge.rx_buf[4]);
    led2_handle_frame(sock, s_bridge.rx_buf[3], seq, &s_bridge.rx_buf[8], payload_len);

    memmove(s_bridge.rx_buf, s_bridge.rx_buf + frame_len, s_bridge.rx_len - frame_len);
    s_bridge.rx_len = (uint16_t)(s_bridge.rx_len - frame_len);
  }

  return 0;
}

static int led2_recv_once(int sock)
{
  int recv_len;

  if (s_bridge.rx_len >= LED2_RX_BUF_LEN) {
    s_bridge.rx_len = 0u;
  }

  recv_len = recv(sock, &s_bridge.rx_buf[s_bridge.rx_len], LED2_RX_BUF_LEN - s_bridge.rx_len, 0);
  if (recv_len > 0) {
    s_bridge.rx_len = (uint16_t)(s_bridge.rx_len + (uint16_t)recv_len);
    return led2_process_rx_buffer(sock);
  }

  if (recv_len == 0) {
    return -1;
  }

  return -1;
}

static int led2_send_hello(int sock)
{
  uint8_t tx_buf[LED2_TX_BUF_LEN];
  uint16_t frame_len;

  frame_len = led2_build_hello_frame(tx_buf, sizeof(tx_buf), s_bridge.next_seq++);
  if (frame_len == 0u) {
    s_bridge.tx_err_cnt++;
    return -1;
  }

  if (led2_send_frame(sock, tx_buf, frame_len) != 0) {
    return -1;
  }

  s_bridge.hello_sent = 1u;
  return 0;
}

static int led2_send_telemetry(int sock)
{
  uint8_t tx_buf[LED2_TX_BUF_LEN];
  uint16_t frame_len;

  frame_len = led2_build_telemetry_frame(tx_buf, sizeof(tx_buf), s_bridge.next_seq++);
  if (frame_len == 0u) {
    s_bridge.tx_err_cnt++;
    return -1;
  }

  return led2_send_frame(sock, tx_buf, frame_len);
}

static void led2_reset_connection_state(void)
{
  s_bridge.hello_sent = 0u;
  s_bridge.last_tx_len = 0u;
  s_bridge.rx_len = 0u;
}

static void client(void *thread_param)
{
  int sock;
  int select_ret;
  struct sockaddr_in server_addr;
  fd_set readfds;
  struct timeval tv;
  ip4_addr_t server_ip;
  uint32_t last_tlm_tick;

  (void)thread_param;

  memset(&s_bridge, 0, sizeof(s_bridge));
  s_bridge.next_seq = 1u;

  PRINTF("LED2 AP6181 bridge target %d.%d.%d.%d:%d\r\n",
         DEST_IP_ADDR0, DEST_IP_ADDR1, DEST_IP_ADDR2, DEST_IP_ADDR3, DEST_PORT);

  IP4_ADDR(&server_ip, DEST_IP_ADDR0, DEST_IP_ADDR1, DEST_IP_ADDR2, DEST_IP_ADDR3);

  for (;;) {
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
      PRINTF("socket create failed\r\n");
      vTaskDelay(pdMS_TO_TICKS(LED2_RECONNECT_DELAY_MS));
      continue;
    }

    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(DEST_PORT);
    server_addr.sin_addr.s_addr = server_ip.addr;

    PRINTF("connecting to server...\r\n");
    if (connect(sock, (struct sockaddr *)&server_addr, sizeof(server_addr)) != 0) {
      PRINTF("connect failed\r\n");
      closesocket(sock);
      vTaskDelay(pdMS_TO_TICKS(LED2_RECONNECT_DELAY_MS));
      continue;
    }

    PRINTF("connected to server\r\n");
    led2_reset_connection_state();
    if (led2_send_hello(sock) != 0) {
      closesocket(sock);
      vTaskDelay(pdMS_TO_TICKS(LED2_RECONNECT_DELAY_MS));
      continue;
    }

    last_tlm_tick = HAL_GetTick();
    if (led2_send_telemetry(sock) != 0) {
      closesocket(sock);
      vTaskDelay(pdMS_TO_TICKS(LED2_RECONNECT_DELAY_MS));
      continue;
    }

    for (;;) {
      if (led2_has_elapsed(HAL_GetTick(), last_tlm_tick, LED2_TLM_PERIOD_MS)) {
        last_tlm_tick = HAL_GetTick();
        if (led2_send_telemetry(sock) != 0) {
          break;
        }
      }

      FD_ZERO(&readfds);
      FD_SET(sock, &readfds);
      tv.tv_sec = 0;
      tv.tv_usec = LED2_SELECT_TIMEOUT_US;

      select_ret = select(sock + 1, &readfds, NULL, NULL, &tv);
      if (select_ret > 0) {
        if (FD_ISSET(sock, &readfds)) {
          if (led2_recv_once(sock) != 0) {
            break;
          }
        }
      } else if (select_ret < 0) {
        break;
      }
    }

    PRINTF("server disconnected, reconnecting...\r\n");
    closesocket(sock);
    vTaskDelay(pdMS_TO_TICKS(LED2_RECONNECT_DELAY_MS));
  }
}

void client_init(void)
{
  sys_thread_new("client", client, NULL, 1024, 4);
}
