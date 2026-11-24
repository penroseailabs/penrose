#!/system/bin/sh
# Magisk runs this before Zygote. -n avoids property-service boot deadlocks.
# Galaxy S25 (SM-S931B, pa1q, Snapdragon 8 Elite), Android 15 firmware S931BXXU1AYA1.
resetprop -n ro.product.brand samsung
resetprop -n ro.product.manufacturer samsung
resetprop -n ro.product.model SM-S931B
resetprop -n ro.product.device pa1q
resetprop -n ro.product.name pa1qxxx
resetprop -n ro.build.product pa1q
resetprop -n ro.build.fingerprint samsung/pa1qxxx/pa1q:15/AP3A.240905.015.A2/S931BXXU1AYA1:user/release-keys
resetprop -n ro.hardware qcom
resetprop -n ro.boot.hardware qcom

# Keep partition-specific product identities consistent with the top-level values.
for partition in system system_ext vendor odm product bootimage; do
    resetprop -n ro.product.$partition.brand samsung
    resetprop -n ro.product.$partition.manufacturer samsung
    resetprop -n ro.product.$partition.model SM-S931B
    resetprop -n ro.product.$partition.device pa1q
    resetprop -n ro.product.$partition.name pa1qxxx
    resetprop -n ro.$partition.build.fingerprint samsung/pa1qxxx/pa1q:15/AP3A.240905.015.A2/S931BXXU1AYA1:user/release-keys
done
