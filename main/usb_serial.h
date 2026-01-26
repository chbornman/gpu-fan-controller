#pragma once

#include "esp_err.h"
#include <stdint.h>
#include <stdbool.h>

/**
 * Command callback type
 * Called when a valid command is received over USB serial
 */
typedef void (*usb_serial_cmd_callback_t)(const char *cmd, const char *arg);

/**
 * Initialize USB CDC serial communication
 * @return ESP_OK on success
 */
esp_err_t usb_serial_init(void);

/**
 * Register callback for incoming commands
 * @param callback Function to call when command received
 */
void usb_serial_set_callback(usb_serial_cmd_callback_t callback);

/**
 * Send a response string over USB serial
 * @param response Null-terminated string to send
 */
void usb_serial_send(const char *response);

/**
 * Send formatted response over USB serial
 * @param fmt printf-style format string
 */
void usb_serial_sendf(const char *fmt, ...);

/**
 * Process incoming data (call from main loop or task)
 * Parses commands and invokes callback
 */
void usb_serial_process(void);
