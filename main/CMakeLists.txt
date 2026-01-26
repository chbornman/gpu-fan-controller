idf_component_register(
    SRCS
        "main.c"
        "fan_pwm.c"
        "fan_tach.c"
        "usb_serial.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_driver_ledc
        esp_driver_gpio
        esp_timer
        nvs_flash
)
