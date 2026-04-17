#include "usb_serial.h"
#include "proto.h"
#include "driver/uart.h"
#include <string.h>

// The dev board routes UART0 through an on-board CH340 USB-UART bridge, so
// "USB" at the host side is really UART0 at the ESP32. We own UART0 here and
// the ESP-IDF console is disabled globally (see sdkconfig.defaults), so
// nothing else writes to this pin pair.

#define UART_PORT      UART_NUM_0
#define UART_BAUD      115200
#define RX_BUF_BYTES   512
#define TX_BUF_BYTES   512

static uint8_t rx_accum[PROTO_MAX_FRAME];
static size_t  rx_accum_len = 0;
static bool    rx_overflow  = false;

static usb_serial_frame_callback_t frame_cb = NULL;

esp_err_t usb_serial_init(void)
{
    const uart_config_t cfg = {
        .baud_rate  = UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_driver_install(UART_PORT, RX_BUF_BYTES, TX_BUF_BYTES,
                                        0, NULL, 0);
    if (err != ESP_OK) return err;
    return uart_param_config(UART_PORT, &cfg);
}

void usb_serial_set_frame_callback(usb_serial_frame_callback_t cb)
{
    frame_cb = cb;
}

esp_err_t usb_serial_send_frame(uint8_t type,
                                const uint8_t *payload, size_t payload_len)
{
    uint8_t frame[PROTO_MAX_FRAME];
    size_t n = proto_encode_frame(type, payload, payload_len, frame, sizeof(frame));
    if (n == 0) return ESP_ERR_INVALID_SIZE;

    int written = uart_write_bytes(UART_PORT, frame, n);
    return (written == (int)n) ? ESP_OK : ESP_ERR_TIMEOUT;
}

static void dispatch_accum(void)
{
    if (rx_overflow) {
        rx_overflow = false;
        rx_accum_len = 0;
        return;
    }
    if (rx_accum_len == 0) return;

    uint8_t type;
    uint8_t payload[PROTO_MAX_PAYLOAD];
    size_t payload_len = sizeof(payload);
    if (proto_decode_frame(rx_accum, rx_accum_len, &type, payload, &payload_len)) {
        if (frame_cb) frame_cb(type, payload, payload_len);
    }
    rx_accum_len = 0;
}

void usb_serial_process(void)
{
    uint8_t buf[64];
    int n;
    while ((n = uart_read_bytes(UART_PORT, buf, sizeof(buf), 0)) > 0) {
        for (int i = 0; i < n; i++) {
            uint8_t b = buf[i];
            if (b == 0x00) {
                dispatch_accum();
            } else if (rx_accum_len < sizeof(rx_accum)) {
                rx_accum[rx_accum_len++] = b;
            } else {
                rx_overflow = true;
            }
        }
    }
}
