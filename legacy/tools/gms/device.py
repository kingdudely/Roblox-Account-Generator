"""Device profiles for the GMS check-in request.

Check-in is the one part of Google's device protocol that is genuinely just
HTTP: a protobuf POST carrying the build fingerprint and a feature list, which
answers with an `android_id` and a `security_token`. Everything the server knows
about the "device" comes from these fields, so they are the fields worth getting
coherent.

Coherence matters more than novelty. A Pixel 8 with a Snapdragon radio and an
`armeabi`-only native platform is a contradiction, and contradictions are free
signal. Each profile here is a single real build: fingerprint, hardware, ABI,
density and screen size all agree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Device:
    key: str
    fingerprint: str
    hardware: str
    brand: str
    radio: str
    bootloader: str
    device: str
    model: str
    manufacturer: str
    product: str
    sdk: int
    density_dpi: int
    width: int
    height: int
    package_version_code: int = 2400000
    client_id: str = "android-google"
    native_platforms: list[str] = field(
        default_factory=lambda: ["arm64-v8a", "armeabi-v7a", "armeabi"])
    shared_libraries: list[str] = field(default_factory=lambda: [
        "android.test.runner",
        "com.android.future.usb.accessory",
        "com.android.location.provider",
        "com.android.media.remotedisplay",
        "com.android.mediadrm.signer",
        "com.google.android.maps",
        "com.google.android.media.effects",
        "com.google.widevine.software.drm",
        "javax.obex",
    ])
    features: list[str] = field(default_factory=lambda: [
        "android.hardware.audio.output",
        "android.hardware.bluetooth",
        "android.hardware.bluetooth_le",
        "android.hardware.camera",
        "android.hardware.camera.any",
        "android.hardware.camera.autofocus",
        "android.hardware.camera.flash",
        "android.hardware.camera.front",
        "android.hardware.fingerprint",
        "android.hardware.location",
        "android.hardware.location.gps",
        "android.hardware.location.network",
        "android.hardware.microphone",
        "android.hardware.nfc",
        "android.hardware.screen.landscape",
        "android.hardware.screen.portrait",
        "android.hardware.sensor.accelerometer",
        "android.hardware.sensor.barometer",
        "android.hardware.sensor.compass",
        "android.hardware.sensor.gyroscope",
        "android.hardware.sensor.light",
        "android.hardware.sensor.proximity",
        "android.hardware.sensor.stepcounter",
        "android.hardware.telephony",
        "android.hardware.telephony.gsm",
        "android.hardware.touchscreen",
        "android.hardware.touchscreen.multitouch",
        "android.hardware.touchscreen.multitouch.distinct",
        "android.hardware.touchscreen.multitouch.jazzhand",
        "android.hardware.usb.accessory",
        "android.hardware.usb.host",
        "android.hardware.wifi",
        "android.hardware.wifi.direct",
        "android.software.device_admin",
        "android.software.leanback",
        "android.software.live_wallpaper",
        "android.software.sip",
        "android.software.sip.voip",
        "com.google.android.feature.GOOGLE_BUILD",
        "com.google.android.feature.GOOGLE_EXPERIENCE",
    ])
    gl_extensions: list[str] = field(default_factory=lambda: [
        "GL_OES_EGL_image",
        "GL_OES_EGL_image_external",
        "GL_OES_depth24",
        "GL_OES_element_index_uint",
        "GL_OES_fragment_precision_high",
        "GL_OES_standard_derivatives",
        "GL_OES_texture_float",
        "GL_OES_texture_half_float",
        "GL_OES_vertex_array_object",
        "GL_EXT_debug_marker",
        "GL_EXT_robustness",
        "GL_EXT_shader_texture_lod",
    ])

    def checkin_user_agent(self) -> str:
        return f"Android-Checkin/2.0 ({self.device} {self.build_id()}); gzip"

    def build_id(self) -> str:
        # fingerprint = brand/product/device:release/id/incremental:type/tags
        parts = self.fingerprint.split("/")
        if len(parts) >= 4:
            return parts[3].split(":")[0]
        return "unknown"

    def gms_version(self) -> str:
        return f"{self.sdk} ({self.build_id()})"


DEVICES: dict[str, Device] = {
    "pixel8": Device(
        key="pixel8",
        fingerprint="google/shiba/shiba:14/UQ1A.240205.004/11206848:user/release-keys",
        hardware="shiba",
        brand="google",
        radio="g5300i-240101-240115-B-11475858",
        bootloader="ripcurrent-14.1-11206848",
        device="shiba",
        model="Pixel 8",
        manufacturer="Google",
        product="shiba",
        sdk=34,
        density_dpi=420,
        width=1080,
        height=2400,
    ),
    "pixel7": Device(
        key="pixel7",
        fingerprint="google/panther/panther:14/UQ1A.240205.004/11206848:user/release-keys",
        hardware="panther",
        brand="google",
        radio="g5300g-231223-231222-B-11076210",
        bootloader="slider-14.3-11206848",
        device="panther",
        model="Pixel 7",
        manufacturer="Google",
        product="panther",
        sdk=34,
        density_dpi=420,
        width=1080,
        height=2400,
    ),
    "pixel6": Device(
        key="pixel6",
        fingerprint="google/oriole/oriole:14/UQ1A.240205.004/11206848:user/release-keys",
        hardware="oriole",
        brand="google",
        radio="g5123b-116534-231222-B-11076210",
        bootloader="slider-14.3-11206848",
        device="oriole",
        model="Pixel 6",
        manufacturer="Google",
        product="oriole",
        sdk=34,
        density_dpi=420,
        width=1080,
        height=2400,
    ),
}


def pick(key: str | None = None) -> Device:
    if key:
        return DEVICES[key]
    return DEVICES["pixel8"]
