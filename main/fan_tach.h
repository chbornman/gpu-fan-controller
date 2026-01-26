#pragma once

#include "esp_err.h"
#include <stdint.h>

// Most 4-pin fans pulse tach line 2 times per revolution
#define TACH_PULSES_PER_REV 2

/**
 * Initialize tachometer input with interrupt-based pulse counting
 * @param gpio_num GPIO pin connected to fan tach wire (green)
 * @return ESP_OK on success
 */
esp_err_t fan_tach_init(int gpio_num);

/**
 * Get current fan RPM
 * Calculated from pulse count over the last measurement window
 * @return RPM (0 if fan stopped or not enough samples)
 */
uint32_t fan_tach_get_rpm(void);

/**
 * Get raw pulse count since last call (for debugging)
 * @return Number of tach pulses
 */
uint32_t fan_tach_get_pulse_count(void);
