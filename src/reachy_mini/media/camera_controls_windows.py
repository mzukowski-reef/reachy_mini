"""Set the Reachy camera's auto-exposure priority on Windows.

Media Foundation's ``mfvideosrc`` exposes no camera controls, so this module
opens a second, control-only DirectShow filter for the same camera and sends
``KSPROPERTY_CAMERACONTROL_AUTO_EXPOSURE_PRIORITY`` through ``IKsControl``. The
Windows Camera Frame Server lets this sharing-mode handle change the control
while the daemon streams.

Windows applies its stored per-user camera defaults whenever an application
starts the camera, so the value has to be set again after every camera start.

All COM objects live on the calling thread, which this module initializes.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_long, c_ulong, c_ushort, c_void_p, c_wchar_p
from typing import Any

assert sys.platform == "win32"

_ole32 = ctypes.windll.ole32
_oleaut32 = ctypes.windll.oleaut32
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = -2147417850
_VT_BSTR = 8
_FLAGS_MANUAL = 0x2
_KSPROPERTY_TYPE_GET = 0x1
_KSPROPERTY_TYPE_SET = 0x2
_KSPROPERTY_CAMERACONTROL_AUTO_EXPOSURE_PRIORITY = 19


class GUID(ctypes.Structure):
    """Windows GUID."""

    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _check(result: int) -> int:
    if result < 0:
        raise OSError(f"HRESULT 0x{result & 0xFFFFFFFF:08X}")
    return result


def _guid(text: str) -> GUID:
    guid = GUID()
    _check(_ole32.CLSIDFromString(c_wchar_p(text), byref(guid)))
    return guid


CLSID_SYSTEM_DEVICE_ENUM = _guid("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
CLSID_VIDEO_INPUT_DEVICE_CATEGORY = _guid("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
IID_ICREATE_DEV_ENUM = _guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
IID_IPROPERTY_BAG = _guid("{55272A00-42CB-11CE-8135-00AA004BB851}")
IID_IBASE_FILTER = _guid("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")
IID_IKS_CONTROL = _guid("{28F54685-06FD-11D2-B27A-00A0C9223196}")
# The KS property set id equals the IAMCameraControl interface id.
PROPSETID_VIDCAP_CAMERACONTROL = _guid("{C6E13370-30AC-11D0-A18C-00A0C9118956}")


class VARIANT(ctypes.Structure):
    """VARIANT holding the BSTR of a property bag entry."""

    _fields_ = [
        ("vt", c_ushort),
        ("reserved1", c_ushort),
        ("reserved2", c_ushort),
        ("reserved3", c_ushort),
        ("value", c_void_p),
        ("padding", c_void_p),
    ]


class KSIDENTIFIER(ctypes.Structure):
    """Property set, id and flags of a KS request."""

    _fields_ = [("Set", GUID), ("Id", c_ulong), ("Flags", c_ulong)]


class KSPROPERTY(ctypes.Union):
    """KS property header, 8-byte aligned by its LONGLONG member as in ks.h."""

    # Without the alignment KSPROPERTY_CAMERACONTROL_S has 36 bytes instead of
    # 40, and drivers refuse it with ERROR_INSUFFICIENT_BUFFER.
    _anonymous_ = ("identifier",)
    _fields_ = [("identifier", KSIDENTIFIER), ("Alignment", ctypes.c_longlong)]


class KSPROPERTY_CAMERACONTROL_S(ctypes.Structure):
    """Camera control request and reply."""

    _fields_ = [
        ("Property", KSPROPERTY),
        ("Value", c_long),
        ("Flags", c_ulong),
        ("Capabilities", c_ulong),
    ]


def _call(
    pointer: c_void_p, index: int, *args: Any, argtypes: tuple[Any, ...] = ()
) -> int:
    """Call vtable method `index` of a COM interface pointer."""
    vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)
    return int(prototype(vtable[index])(pointer, *args))


def _release(pointer: c_void_p | None) -> None:
    if pointer:
        _call(pointer, 2)


def _friendly_name(moniker: c_void_p) -> str:
    bag = c_void_p()
    # IMoniker::BindToStorage(pbc, pmkToLeft, riid, ppvObj)
    _check(
        _call(
            moniker,
            9,
            None,
            None,
            byref(IID_IPROPERTY_BAG),
            byref(bag),
            argtypes=(c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)),
        )
    )
    try:
        variant = VARIANT()
        variant.vt = _VT_BSTR
        # IPropertyBag::Read(name, variant, error_log)
        _check(
            _call(
                bag,
                3,
                c_wchar_p("FriendlyName"),
                byref(variant),
                None,
                argtypes=(c_wchar_p, POINTER(VARIANT), c_void_p),
            )
        )
        name = ctypes.wstring_at(variant.value) if variant.value else ""
        _oleaut32.VariantClear(byref(variant))
        return name
    finally:
        _release(bag)


def _find_filter(name: str) -> c_void_p:
    """Find the DirectShow capture filter whose friendly name contains `name`."""
    device_enum = c_void_p()
    _check(
        _ole32.CoCreateInstance(
            byref(CLSID_SYSTEM_DEVICE_ENUM),
            None,
            _CLSCTX_INPROC_SERVER,
            byref(IID_ICREATE_DEV_ENUM),
            byref(device_enum),
        )
    )
    monikers = c_void_p()
    try:
        # ICreateDevEnum::CreateClassEnumerator(category, enum, flags); S_FALSE: no devices.
        status = _check(
            _call(
                device_enum,
                3,
                byref(CLSID_VIDEO_INPUT_DEVICE_CATEGORY),
                byref(monikers),
                0,
                argtypes=(POINTER(GUID), POINTER(c_void_p), c_ulong),
            )
        )
    finally:
        _release(device_enum)
    if status != 0 or not monikers:
        raise RuntimeError("Windows reports no video capture devices")
    seen = []
    try:
        while True:
            moniker, fetched = c_void_p(), c_ulong()
            # IEnumMoniker::Next(count, monikers, fetched)
            status = _call(
                monikers,
                3,
                1,
                byref(moniker),
                byref(fetched),
                argtypes=(c_ulong, POINTER(c_void_p), POINTER(c_ulong)),
            )
            if status != 0 or not fetched.value:
                break
            try:
                friendly = _friendly_name(moniker)
                seen.append(friendly)
                if name not in friendly:
                    continue
                camera_filter = c_void_p()
                # IMoniker::BindToObject(pbc, pmkToLeft, riid, ppvResult)
                _check(
                    _call(
                        moniker,
                        8,
                        None,
                        None,
                        byref(IID_IBASE_FILTER),
                        byref(camera_filter),
                        argtypes=(c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)),
                    )
                )
                return camera_filter
            finally:
                _release(moniker)
    finally:
        _release(monikers)
    raise RuntimeError(
        f"No DirectShow camera named {name!r}; Windows sees: {', '.join(seen) or 'none'}"
    )


def _camera_control(ks: c_void_p, property_id: int, flags: int, value: int = 0) -> int:
    request = KSPROPERTY_CAMERACONTROL_S()
    request.Property.Set = PROPSETID_VIDCAP_CAMERACONTROL
    request.Property.Id = property_id
    request.Property.Flags = flags
    request.Value = value
    request.Flags = _FLAGS_MANUAL
    returned = c_ulong()
    size = ctypes.sizeof(request)
    # IKsControl::KsProperty(property, length, data, data_length, returned)
    _check(
        _call(
            ks,
            3,
            byref(request),
            size,
            byref(request),
            size,
            byref(returned),
            argtypes=(c_void_p, c_ulong, c_void_p, c_ulong, POINTER(c_ulong)),
        )
    )
    return int(request.Value)


def set_auto_exposure_priority(device_name: str, enabled: bool) -> int:
    """Allow (or forbid) automatic exposure to lower the frame rate.

    Returns the value the camera reports after the write.
    """
    status = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if status < 0 and status != _RPC_E_CHANGED_MODE:
        _check(status)
    try:
        camera_filter = _find_filter(device_name)
        ks = c_void_p()
        try:
            _check(
                _call(
                    camera_filter,
                    0,
                    byref(IID_IKS_CONTROL),
                    byref(ks),
                    argtypes=(POINTER(GUID), POINTER(c_void_p)),
                )
            )
            try:
                _camera_control(
                    ks,
                    _KSPROPERTY_CAMERACONTROL_AUTO_EXPOSURE_PRIORITY,
                    _KSPROPERTY_TYPE_SET,
                    int(enabled),
                )
                return _camera_control(
                    ks,
                    _KSPROPERTY_CAMERACONTROL_AUTO_EXPOSURE_PRIORITY,
                    _KSPROPERTY_TYPE_GET,
                )
            finally:
                _release(ks)
        finally:
            _release(camera_filter)
    finally:
        if status >= 0:
            _ole32.CoUninitialize()
