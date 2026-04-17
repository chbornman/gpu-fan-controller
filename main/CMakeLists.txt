idf_component_register(
    SRCS
        "main.c"
        "fan_pwm.c"
        "fan_tach.c"
        "usb_serial.c"
        "cobs.c"
        "proto.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_driver_ledc
        esp_driver_gpio
        esp_driver_uart
        esp_timer
        nvs_flash
)
