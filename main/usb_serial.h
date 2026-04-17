#pragma once

#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>

// Install the UART driver for the host protocol link. The board's USB port
// bridges to UART0 via an on-board CH340, and ESP-IDF console is disabled
// globally, so this module owns UART0 outright.
esp_err_t usb_serial_init(void);

typedef void (*usb_serial_frame_callback_t)(uint8_t type,
                                            const uint8_t *payload,
                                            size_t payload_len);

void usb_serial_set_frame_callback(usb_serial_frame_callback_t cb);

// Send one COBS-framed message. Non-blocking — drops if the TX buffer is full.
esp_err_t usb_serial_send_frame(uint8_t type,
                                const uint8_t *payload, size_t payload_len);

// Drain the RX buffer and fire the frame callback for each complete frame.
// Call frequently from the main loop.
void usb_serial_process(void);
