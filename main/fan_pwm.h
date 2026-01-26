#pragma once

#include "esp_err.h"
#include <stdint.h>

// 4-pin PWM fans expect 25kHz PWM signal (Intel spec)
#define FAN_PWM_FREQ_HZ 25000

/**
 * Initialize the LEDC peripheral for fan PWM control
 * @param gpio_num GPIO pin connected to fan PWM wire (blue)
 * @return ESP_OK on success
 */
esp_err_t fan_pwm_init(int gpio_num);

/**
 * Set fan speed as a percentage
 * @param percent 0-100 (0 = fan may stop or run minimum, 100 = full speed)
 */
void fan_pwm_set_percent(uint8_t percent);

/**
 * Get current fan speed setting
 * @return Current speed percentage (0-100)
 */
uint8_t fan_pwm_get_percent(void);
