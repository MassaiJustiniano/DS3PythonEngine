# -*- coding: utf-8 -*-
"""
DS3 Data0 Reader v4
====================

Lector EXCLUSIVO del Data0.bdt vanilla de Dark Souls III.

No modifica:
    - Game\\Data0.bdt
    - ningún Data0 de mods
    - ningún MSB
    - ningún PARAM

Su única salida es un JSON de diagnóstico/identificación en:
    <MOD_DIRECTORY>\\EnemyTables\\data0_original.json

Pipeline de lectura:
    Data0.bdt
        |
        +--> AES-256-CBC
        |      IV = primeros 16 bytes
        |      Key = clave DS3 conocida
        |
        +--> BND4
               |
               +--> entries
                      |
                      +--> DCX si la entrada está comprimida
                      |
                      +--> BND4 anidado
                      |
                      +--> PARAM
                             |
                             +--> ParamType
                             +--> ParamdefDataVersion
                             +--> Format flags
                             +--> Row IDs
                             +--> Row names
                             +--> Row offsets
                             +--> Row size

El JSON NO almacena todos los bytes de los archivos internos.
Guarda offsets, tamaños, nombres, IDs, hashes, magics, formatos y
metadatos PARAM suficientes para que un futuro editor pueda volver a
abrir Data0.bdt y modificar entradas concretas de forma reproducible.

IMPORTANTE:
    Este programa está hecho para probar y documentar el formato real.
    Si una variante del Data0 no coincide exactamente con una estructura
    conocida, el programa registra el error y continúa donde sea posible.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import struct
import sys
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import ds3_engine_config as engine_config


# ============================================================================
# CONFIGURACIÓN
# ============================================================================
#
# Todas las rutas se calculan desde PythonEngineDS3, que debe estar dentro
# de Game. La configuración compartida está en ds3_engine_config.py.
#
# Data0.bdt es la entrada efectiva del lector.
# Data0.bhd se registra como archivo compañero, pero no se interpreta.

GAME_DIRECTORY_PATH = Path()
DATA0_PATH = Path()
DATA0_BHD_PATH = Path()
MOD_DIRECTORY_PATH = Path()
OUTPUT_DIRECTORY = Path()
OUTPUT_PATH = Path()
STRICT_GAME_DATA0_ONLY = True

MAX_RECURSION_DEPTH = 8
INCLUDE_RAW_PREVIEW_HEX = True
RAW_PREVIEW_BYTES = 32
INCLUDE_ROW_NAMES = True
INCLUDE_ROW_DATA_PREVIEW = False
ROW_DATA_PREVIEW_BYTES = 16

# Definiciones mínimas de los PARAM que necesitamos para validar y extraer
# identificadores usados por el editor de enemigos. Estas estructuras coinciden
# con los PARAMDEF de DS3 utilizados por DSMapStudio/Paramdex.
TARGET_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "NPC_PARAM_ST": {
        "data_version": 9,
        "row_size": 0x244,
        "fields": {
            "BehaviorVariationId": {"offset": 0x00, "type": "int32"},
            "AiThinkId": {"offset": 0x04, "type": "int32"},
            "NameId": {"offset": 0x08, "type": "int32"},
            "Hp": {"offset": 0x20, "type": "int32"},
            "Mp": {"offset": 0x24, "type": "int32"},
            "getSoul": {"offset": 0x28, "type": "int32"},
            "ItemLotId1": {"offset": 0x2C, "type": "int32"},
            "ItemLotId2": {"offset": 0x30, "type": "int32"},
            "ItemLotId3": {"offset": 0x34, "type": "int32"},
            "ItemLotId4": {"offset": 0x38, "type": "int32"},
            "ItemLotId5": {"offset": 0x3C, "type": "int32"},
            "ItemLotId6": {"offset": 0x40, "type": "int32"},
            "disableRespawn": {"offset": 0x148, "type": "bit", "bit": 1},
        },
    },
    "NPC_THINK_PARAM_ST": {
        "data_version": 1,
        "row_size": 0xAC,
        "fields": {
            "logicId": {"offset": 0x00, "type": "int32"},
            "battleGoalId": {"offset": 0x04, "type": "int32"},
            "nearDist": {"offset": 0x08, "type": "float32"},
            "midDist": {"offset": 0x0C, "type": "float32"},
            "farDist": {"offset": 0x10, "type": "float32"},
        },
    },
    "ITEMLOT_PARAM_ST": {
        "data_version": 2,
        "row_size": 0x98,
        "fields": {
            "ItemLotId1": {"offset": 0x00, "type": "int32"},
            "ItemLotId2": {"offset": 0x04, "type": "int32"},
            "ItemLotId3": {"offset": 0x08, "type": "int32"},
            "ItemLotId4": {"offset": 0x0C, "type": "int32"},
            "ItemLotId5": {"offset": 0x10, "type": "int32"},
            "ItemLotId6": {"offset": 0x14, "type": "int32"},
            "ItemLotId7": {"offset": 0x18, "type": "int32"},
            "ItemLotId8": {"offset": 0x1C, "type": "int32"},
            "LotItemCategory01": {"offset": 0x20, "type": "uint32"},
            "LotItemCategory02": {"offset": 0x24, "type": "uint32"},
            "LotItemCategory03": {"offset": 0x28, "type": "uint32"},
            "LotItemCategory04": {"offset": 0x2C, "type": "uint32"},
            "LotItemCategory05": {"offset": 0x30, "type": "uint32"},
            "LotItemCategory06": {"offset": 0x34, "type": "uint32"},
            "LotItemCategory07": {"offset": 0x38, "type": "uint32"},
            "LotItemCategory08": {"offset": 0x3C, "type": "uint32"},
            "LotItemBasePoint01": {"offset": 0x40, "type": "uint16"},
            "LotItemBasePoint02": {"offset": 0x42, "type": "uint16"},
            "LotItemBasePoint03": {"offset": 0x44, "type": "uint16"},
            "LotItemBasePoint04": {"offset": 0x46, "type": "uint16"},
            "LotItemBasePoint05": {"offset": 0x48, "type": "uint16"},
            "LotItemBasePoint06": {"offset": 0x4A, "type": "uint16"},
            "LotItemBasePoint07": {"offset": 0x4C, "type": "uint16"},
            "LotItemBasePoint08": {"offset": 0x4E, "type": "uint16"},
            "cumulateLotPoint01": {"offset": 0x50, "type": "uint16"},
            "cumulateLotPoint02": {"offset": 0x52, "type": "uint16"},
            "cumulateLotPoint03": {"offset": 0x54, "type": "uint16"},
            "cumulateLotPoint04": {"offset": 0x56, "type": "uint16"},
            "cumulateLotPoint05": {"offset": 0x58, "type": "uint16"},
            "cumulateLotPoint06": {"offset": 0x5A, "type": "uint16"},
            "cumulateLotPoint07": {"offset": 0x5C, "type": "uint16"},
            "cumulateLotPoint08": {"offset": 0x5E, "type": "uint16"},
            "LotItemNum1": {"offset": 0x8A, "type": "uint8"},
            "LotItemNum2": {"offset": 0x8B, "type": "uint8"},
            "LotItemNum3": {"offset": 0x8C, "type": "uint8"},
            "LotItemNum4": {"offset": 0x8D, "type": "uint8"},
            "LotItemNum5": {"offset": 0x8E, "type": "uint8"},
            "LotItemNum6": {"offset": 0x8F, "type": "uint8"},
            "LotItemNum7": {"offset": 0x90, "type": "uint8"},
            "LotItemNum8": {"offset": 0x91, "type": "uint8"},
            "EnableLuckRaw": {"offset": 0x92, "type": "uint8"},
            "cumulateResetRaw": {"offset": 0x93, "type": "uint8"},
            "ClearCount": {"offset": 0x94, "type": "int8"},
            "cumulateNumFlagId": {"offset": 0x84, "type": "int32"},
            "cumulateNumMax": {"offset": 0x88, "type": "uint8"},
            "getItemFlagId": {"offset": 0x80, "type": "int32"},
        },
    },
}

# La búsqueda de otra fuente NO está activa: este lector se dedica
# exclusivamente al Data0 original de GAME_DIRECTORY.
STRICT_GAME_DATA0_ONLY = True


# ============================================================================
# CONSTANTES
# ============================================================================

DS3_REGULATION_KEY = b"ds3#jn/8_7(rsY9pg55GFN7VFL#+3n/)"
BND4_MAGIC = b"BND4"
DCX_MAGIC = b"DCX\x00"
PARAM_MAGIC_CANDIDATES = {
    b"PARAM",  # diagnóstico
}


# ============================================================================
# UTILIDADES
# ============================================================================

class ParseError(RuntimeError):
    pass


def ensure_range(data: bytes, offset: int, size: int, label: str = "") -> None:
    if offset < 0 or offset + size > len(data):
        suffix = f" ({label})" if label else ""
        raise ParseError(
            f"Lectura fuera de rango: offset=0x{offset:X}, "
            f"size=0x{size:X}{suffix}"
        )


def u8(data: bytes, offset: int) -> int:
    ensure_range(data, offset, 1)
    return data[offset]


def u16(data: bytes, offset: int, endian: str) -> int:
    ensure_range(data, offset, 2)
    return int.from_bytes(data[offset:offset + 2], endian, signed=False)


def s16(data: bytes, offset: int, endian: str) -> int:
    ensure_range(data, offset, 2)
    return int.from_bytes(data[offset:offset + 2], endian, signed=True)


def u32(data: bytes, offset: int, endian: str) -> int:
    ensure_range(data, offset, 4)
    return int.from_bytes(data[offset:offset + 4], endian, signed=False)


def s32(data: bytes, offset: int, endian: str) -> int:
    ensure_range(data, offset, 4)
    return int.from_bytes(data[offset:offset + 4], endian, signed=True)


def u64(data: bytes, offset: int, endian: str) -> int:
    ensure_range(data, offset, 8)
    return int.from_bytes(data[offset:offset + 8], endian, signed=False)


def reverse_bits_byte(value: int) -> int:
    value &= 0xFF
    value = ((value & 0x55) << 1) | ((value >> 1) & 0x55)
    value = ((value & 0x33) << 2) | ((value >> 2) & 0x33)
    value = ((value & 0x0F) << 4) | ((value >> 4) & 0x0F)
    return value


def read_cstring(
    data: bytes,
    offset: int,
    encoding: str = "ascii",
    max_bytes: int | None = None,
) -> str | None:
    if offset <= 0 or offset >= len(data):
        return None

    end_limit = len(data) if max_bytes is None else min(len(data), offset + max_bytes)
    end = data.find(b"\x00", offset, end_limit)
    if end == -1:
        end = end_limit

    raw = data[offset:end]
    if not raw:
        return ""

    try:
        return raw.decode(encoding, errors="replace")
    except Exception:
        return None


def read_utf16z(data: bytes, offset: int, endian: str = "little") -> str | None:
    if offset <= 0 or offset >= len(data):
        return None

    end = offset
    while end + 1 < len(data):
        if data[end:end + 2] == b"\x00\x00":
            break
        end += 2

    raw = data[offset:end]
    if not raw:
        return ""

    enc = "utf-16-be" if endian == "big" else "utf-16-le"
    try:
        return raw.decode(enc, errors="replace")
    except Exception:
        return None


def safe_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def magic_name(data: bytes) -> str:
    if len(data) >= 4 and data[:4] == BND4_MAGIC:
        return "BND4"
    if len(data) >= 4 and data[:4] == DCX_MAGIC:
        return "DCX"
    if len(data) >= 4 and data[:4] == b"MSB ":
        return "MSB"
    if len(data) >= 4:
        try:
            ascii4 = data[:4].decode("ascii")
            if all(32 <= ord(ch) < 127 for ch in ascii4):
                return ascii4
        except Exception:
            pass
    return "UNKNOWN"


def _plausible_param_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 64:
        return False
    # PARAMTYPE strings in DS3 son identificadores ASCII en mayúsculas. No
    # imponemos un sufijo concreto: existen nombres como SHOP_LINEUP_PARAM y
    # MENU_VALUE_TABLE_SPEC además de los habituales *_PARAM_ST.
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        return False
    return "_" in value or value.endswith("PARAM") or value.endswith("BANK")


def classify_payload(data: bytes) -> str:
    if data.startswith(BND4_MAGIC):
        return "BND4"
    if data.startswith(DCX_MAGIC):
        return "DCX"
    if data.startswith(b"MSB "):
        return "MSB"
    if len(data) >= 0x40:
        try:
            info = parse_param_header(data)
            if _plausible_param_type(info.get("param_type")):
                return "PARAM"
        except Exception:
            pass
    return "UNKNOWN"


# ============================================================================
# AES-256-CBC DE REGULATION DS3
# ============================================================================

def _raise_ntstatus(name: str, status: int) -> None:
    if status < 0:
        raise ParseError(
            f"{name} falló en bcrypt.dll: "
            f"NTSTATUS=0x{status & 0xFFFFFFFF:08X}"
        )


def decrypt_data0(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """
    Replica el comportamiento documentado por SoulsFormats:
      - IV = primeros 16 bytes
      - ciphertext = resto
      - AES-256-CBC
      - PaddingMode.None
      - si el ciphertext no es múltiplo de 16, se completa con ceros
    """
    if len(data) < 16:
        raise ParseError("Data0.bdt es demasiado pequeño.")

    iv = data[:16]
    ciphertext = data[16:]
    original_ciphertext_len = len(ciphertext)

    if len(ciphertext) % 16:
        padded_len = (len(ciphertext) + 15) & ~15
        ciphertext = ciphertext + b"\x00" * (padded_len - len(ciphertext))
    else:
        padded_len = len(ciphertext)

    if sys.platform != "win32":
        raise ParseError("El descifrado de Data0 requiere Windows.")

    bcrypt = ctypes.WinDLL("bcrypt.dll")

    BCRYPT_ALG_HANDLE = ctypes.c_void_p
    BCRYPT_KEY_HANDLE = ctypes.c_void_p

    alg = BCRYPT_ALG_HANDLE()
    key = BCRYPT_KEY_HANDLE()
    key_object = None

    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(BCRYPT_ALG_HANDLE),
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    ]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_long

    bcrypt.BCryptSetProperty.argtypes = [
        BCRYPT_ALG_HANDLE,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    bcrypt.BCryptSetProperty.restype = ctypes.c_long

    bcrypt.BCryptGetProperty.argtypes = [
        BCRYPT_ALG_HANDLE,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]
    bcrypt.BCryptGetProperty.restype = ctypes.c_long

    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        BCRYPT_ALG_HANDLE,
        ctypes.POINTER(BCRYPT_KEY_HANDLE),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_long

    bcrypt.BCryptDecrypt.argtypes = [
        BCRYPT_KEY_HANDLE,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]
    bcrypt.BCryptDecrypt.restype = ctypes.c_long

    bcrypt.BCryptDestroyKey.argtypes = [BCRYPT_KEY_HANDLE]
    bcrypt.BCryptDestroyKey.restype = ctypes.c_long

    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [
        BCRYPT_ALG_HANDLE,
        ctypes.c_ulong,
    ]
    bcrypt.BCryptCloseAlgorithmProvider.restype = ctypes.c_long

    try:
        _raise_ntstatus(
            "BCryptOpenAlgorithmProvider",
            bcrypt.BCryptOpenAlgorithmProvider(
                ctypes.byref(alg),
                "AES",
                None,
                0,
            ),
        )

        chaining = ctypes.create_unicode_buffer("ChainingModeCBC")
        _raise_ntstatus(
            "BCryptSetProperty",
            bcrypt.BCryptSetProperty(
                alg,
                "ChainingMode",
                ctypes.cast(chaining, ctypes.c_void_p),
                ctypes.sizeof(chaining),
                0,
            ),
        )

        object_length = ctypes.c_ulong(0)
        result_length = ctypes.c_ulong(0)
        _raise_ntstatus(
            "BCryptGetProperty",
            bcrypt.BCryptGetProperty(
                alg,
                "ObjectLength",
                ctypes.byref(object_length),
                ctypes.sizeof(object_length),
                ctypes.byref(result_length),
                0,
            ),
        )

        key_object = ctypes.create_string_buffer(object_length.value)
        key_buffer = ctypes.create_string_buffer(DS3_REGULATION_KEY)
        _raise_ntstatus(
            "BCryptGenerateSymmetricKey",
            bcrypt.BCryptGenerateSymmetricKey(
                alg,
                ctypes.byref(key),
                ctypes.cast(key_object, ctypes.c_void_p),
                object_length.value,
                ctypes.cast(key_buffer, ctypes.c_void_p),
                len(DS3_REGULATION_KEY),
                0,
            ),
        )

        cipher_buffer = ctypes.create_string_buffer(ciphertext)
        iv_buffer = ctypes.create_string_buffer(iv)
        plain_buffer = ctypes.create_string_buffer(len(ciphertext))
        written = ctypes.c_ulong(0)

        _raise_ntstatus(
            "BCryptDecrypt",
            bcrypt.BCryptDecrypt(
                key,
                ctypes.cast(cipher_buffer, ctypes.c_void_p),
                len(ciphertext),
                None,
                ctypes.cast(iv_buffer, ctypes.c_void_p),
                len(iv),
                ctypes.cast(plain_buffer, ctypes.c_void_p),
                len(ciphertext),
                ctypes.byref(written),
                0,
            ),
        )

        plain = plain_buffer.raw[:written.value]

    finally:
        if key:
            bcrypt.BCryptDestroyKey(key)
        if alg:
            bcrypt.BCryptCloseAlgorithmProvider(alg, 0)

    return plain, {
        "iv_hex": iv.hex(),
        "ciphertext_size": original_ciphertext_len,
        "ciphertext_size_after_zero_padding": padded_len,
        "key_size_bytes": len(DS3_REGULATION_KEY),
        "mode": "AES-256-CBC",
        "padding_mode": "None / zero-pad ciphertext to block size",
    }


# ============================================================================
# DCX
# ============================================================================

def decompress_dcx(data: bytes) -> tuple[bytes, dict[str, Any]]:
    if not data.startswith(DCX_MAGIC):
        return data, {
            "was_dcx": False,
            "compression": None,
        }

    if len(data) < 0x4C:
        raise ParseError("DCX demasiado pequeño.")

    compression = data[0x28:0x2C]
    if compression != b"DFLT":
        raise ParseError(
            f"DCX no soportado en este lector: {compression!r}"
        )

    uncompressed_size = u32(data, 0x1C, "big")
    compressed_size = u32(data, 0x20, "big")

    if data[0x44:0x48] != b"DCA\x00":
        raise ParseError("DCX DFLT de DS3 sin DCA en 0x44.")

    start = 0x4C
    ensure_range(data, start, compressed_size, "DCX payload")
    compressed = data[start:start + compressed_size]

    try:
        result = zlib.decompress(compressed)
    except zlib.error:
        result = zlib.decompress(compressed, -15)

    return result, {
        "was_dcx": True,
        "compression": "DCX_DFLT_10000_44_9",
        "declared_uncompressed_size": uncompressed_size,
        "compressed_size": compressed_size,
        "decompressed_size": len(result),
        "declared_size_matches": (uncompressed_size == len(result)),
        "payload_offset": start,
    }


# ============================================================================
# BND4 — VARIANTE REAL USADA POR DS3 DATA0
# ============================================================================
#
# IMPORTANTE:
# DS3 Data0 utiliza una variante BND4 antigua/específica de DS3.
# No debemos aplicar aquí la interpretación genérica moderna de los flags
# de Binder (en especial LongOffsets y BitBigEndian).
#
# La estructura validada por implementaciones DS3 de SoulsFormats/MVDX y por
# BinderTool es:
#   0x08 : marcador endian de 32 bits (0x00010000 LE en PC)
#   0x0C : fileCount
#   0x10 : headerSize (= 0x40)
#   0x18 : versión
#   0x20 : fileHeaderSize (= 0x18, 0x1C o 0x24)
#   0x28 : dataStart
#   0x30 : Unicode
#   0x31 : Format (se usa TAL CUAL; no inversión de bits aquí)
#   0x32 : Extended
#   0x38 : HashGroups offset si Extended == 4
#
# Para el caso DS3 habitual Format=0x26/0x2A/0x2E/0x54/0x74:
#   file header = 0x24 bytes
#   +0x00 flags (1) + padding (3)
#   +0x04 -1 (int32)
#   +0x08 compressedSize (int64)
#   +0x10 uncompressedSize (int64)
#   +0x18 dataOffset (UINT32, NO uint64)
#   +0x1C file ID (int32)
#   +0x20 nameOffset (uint32)
#
# El bug anterior consistía en interpretar +0x18 como uint64 cuando el
# formato 0x74 de DS3 lo usa como uint32. Eso mezclaba dataOffset + ID y
# generaba offsets falsos como 0x00000001000051A0.

DS3_BND4_FORMAT_0X24 = {0x26, 0x2A, 0x2E, 0x54, 0x74}
DS3_BND4_FORMAT_0X1C = {0x70}
DS3_BND4_FORMAT_0X18 = {0x0C}
DS3_BND4_COMPRESSED_FLAGS = {0x03, 0xC0}

BINDER_FILE_FLAGS = {
    0x00: "None",
    0x02: "Flag1",
    0x03: "Compressed|Flag1",
    0x0A: "Compressed|Flag4",
    0x40: "Flag6",
    0xC0: "Compressed|Flag7",
}


def bnd4_format_flags(effective_format: int) -> list[str]:
    # Binder.Format de SoulsFormats: IDs=0x02, Names1=0x04, Names2=0x08,
    # LongOffsets=0x10, Compression=0x20, Flag6=0x40, Flag7=0x80.
    labels = [
        (0x01, "BigEndian"),
        (0x02, "IDs"),
        (0x04, "Names1"),
        (0x08, "Names2"),
        (0x10, "LongOffsets"),
        (0x20, "Compression"),
        (0x40, "Flag6"),
        (0x80, "Flag7"),
    ]
    return [label for bit, label in labels if effective_format & bit]


def ds3_bnd4_format_info(format_byte: int) -> dict[str, Any]:
    if format_byte in DS3_BND4_FORMAT_0X24:
        return {
            "header_size": 0x24,
            "has_uncompressed_size": True,
            "has_ids": True,
            "has_names": True,
            "data_offset_size": 4,
            "variant": "DS3_BND4_FILE_HEADER_0x24",
        }
    if format_byte in DS3_BND4_FORMAT_0X1C:
        return {
            "header_size": 0x1C,
            "has_uncompressed_size": False,
            "has_ids": True,
            "has_names": True,
            "data_offset_size": 4,
            "variant": "DS3_BND4_FILE_HEADER_0x1C",
        }
    if format_byte in DS3_BND4_FORMAT_0X18:
        return {
            "header_size": 0x18,
            "has_uncompressed_size": False,
            "has_ids": False,
            "has_names": True,
            "data_offset_size": 4,
            "variant": "DS3_BND4_FILE_HEADER_0x18",
        }
    # Newer SoulsFormats exposes the semantic Binder.Format value after
    # optional bit reversal. Accept the common DS3 semantic equivalents too.
    semantic = {0x2E: 0x74, 0x2A: 0x54, 0x26: 0x26, 0x70: 0x70, 0x0C: 0x0C}
    if format_byte in semantic:
        raw_equiv = semantic[format_byte]
        info = ds3_bnd4_format_info(raw_equiv)
        info = dict(info)
        info["semantic_format_input"] = True
        return info
    raise ParseError(f"BND4 DS3 Format no soportado: 0x{format_byte:02X}")


def parse_ds3_bnd4_file_flags(raw_flags: int) -> tuple[int, list[str]]:
    # En esta variante los flags del archivo se almacenan/leen directamente.
    names = []
    for bit, label in [
        (0x01, "Compressed"),
        (0x02, "Flag1"),
        (0x04, "Flag2"),
        (0x08, "Flag3"),
        (0x10, "Flag4"),
        (0x20, "Flag5"),
        (0x40, "Flag6"),
        (0x80, "Flag7"),
    ]:
        if raw_flags & bit:
            names.append(label)
    return raw_flags, names


def try_decompress_bnd_entry(payload: bytes) -> tuple[bytes, dict[str, Any]]:
    """Decompresión de entrada BND4 DS3.

    En el formato observado por las implementaciones DS3, flags 0x03/0xC0
    significan payload zlib/deflate. También aceptamos DCX si aparece para
    no perder información durante la exploración forense.
    """
    if payload.startswith(DCX_MAGIC):
        result, info = decompress_dcx(payload)
        info = dict(info)
        info["container"] = "DCX"
        return result, info

    errors = []
    for wbits, label in [(zlib.MAX_WBITS, "zlib"), (-15, "raw_deflate")]:
        try:
            result = zlib.decompress(payload, wbits)
            return result, {
                "was_compressed": True,
                "container": label,
                "compressed_size": len(payload),
                "decompressed_size": len(result),
            }
        except zlib.error as exc:
            errors.append(str(exc))

    raise ParseError(
        "Entrada marcada como comprimida pero no pudo descomprimirse: "
        + " | ".join(errors[:2])
    )


def parse_bnd4(
    data: bytes,
    source_path: str,
    depth: int,
    json_root: dict[str, Any],
) -> dict[str, Any]:
    if not data.startswith(BND4_MAGIC):
        raise ParseError(f"{source_path}: no comienza por BND4.")

    if len(data) < 0x40:
        raise ParseError(f"{source_path}: BND4 demasiado pequeño.")

    # BND4 DS3: 0x08-0x0B contiene tres flags/bytes de control.
    # En el Data0 de PC observado: 00 00 01 00 -> little endian,
    # BitBigEndian=False, por lo que el Format byte se invierte para obtener
    # el valor semántico de Binder.Format (0x74 -> 0x2E).
    raw_control = data[0x08:0x0C]
    if len(raw_control) != 4:
        raise ParseError(f"{source_path}: control bytes BND4 insuficientes.")

    endian = "big" if raw_control[1] else "little"
    big_endian = bool(raw_control[1])
    bit_big_endian = not bool(raw_control[2])
    endian_marker = u32(data, 0x08, "little")

    if big_endian:
        # Para big-endian, el byte-level interpretation anterior tampoco
        # debe reutilizarse como uint32 little-endian. Guardamos ambos.
        endian_marker_be = u32(data, 0x08, "big")
    else:
        endian_marker_be = None

    unk04 = u8(data, 0x04)
    unk05 = u8(data, 0x05)
    file_count = u32(data, 0x0C, endian)
    header_size = u64(data, 0x10, endian)
    version_raw = data[0x18:0x20]
    file_header_size = u64(data, 0x20, endian)
    data_start = u64(data, 0x28, endian)
    unicode_names = bool(u8(data, 0x30))
    raw_format = u8(data, 0x31)
    effective_format = raw_format if bit_big_endian else reverse_bits_byte(raw_format)
    # El parser DS3 legacy se guía por el byte raw para el tamaño de header,
    # mientras que el significado de los bits se expresa con effective_format.
    fmt = ds3_bnd4_format_info(raw_format)
    extended = u8(data, 0x32)
    zero_33 = u8(data, 0x33)
    zero_34_37 = data[0x34:0x38]
    hash_table_offset = u64(data, 0x38, endian)

    if file_count <= 0 or file_count > 1_000_000:
        raise ParseError(
            f"{source_path}: fileCount sospechoso: {file_count}"
        )

    if header_size != 0x40:
        raise ParseError(
            f"{source_path}: headerSize inesperado: 0x{header_size:X}"
        )

    if file_header_size != fmt["header_size"]:
        raise ParseError(
            f"{source_path}: fileHeaderSize=0x{file_header_size:X}, "
            f"pero Format=0x{raw_format:02X} requiere 0x{fmt['header_size']:X}."
        )

    version = version_raw.split(b"\x00", 1)[0].decode(
        "ascii", errors="replace"
    )

    result: dict[str, Any] = {
        "kind": "BND4",
        "source_path": source_path,
        "depth": depth,
        "size_bytes": len(data),
        "sha256": safe_sha256(data),
        "header": {
            "magic": "BND4",
            "variant": "Dark Souls III legacy BND4",
            "unk04": unk04,
            "unk05": unk05,
            "endian_marker_0x08": f"0x{endian_marker:08X}",
            "control_bytes_0x08_0x0B": raw_control.hex(),
            "big_endian": big_endian,
            "endian_marker_big_endian_interpretation": (
                f"0x{endian_marker_be:08X}" if endian_marker_be is not None else None
            ),
            "endian": endian,
            "file_count": file_count,
            "header_size": header_size,
            "version": version,
            "file_header_size": file_header_size,
            "data_start": f"0x{data_start:X}",
            "unicode_names": unicode_names,
            "bit_big_endian": bit_big_endian,
            "format_raw": raw_format,
            "format_effective": effective_format,
            "format_effective_hex": f"0x{effective_format:02X}",
            "format_flags": bnd4_format_flags(effective_format),
            "format_variant": fmt["variant"],
            "has_uncompressed_size": fmt["has_uncompressed_size"],
            "data_offset_size_bytes": fmt["data_offset_size"],
            "extended": extended,
            "byte_0x33": zero_33,
            "bytes_0x34_0x37": zero_34_37.hex(),
            "hash_table_offset": f"0x{hash_table_offset:X}",
        },
        "entries": [],
    }

    # Un diagnóstico directo de las zonas clave antes de interpretar entradas.
    sample_headers = []
    for sample_index in range(min(file_count, 8)):
        sample_base = 0x40 + sample_index * int(file_header_size)
        sample_end = min(len(data), sample_base + int(file_header_size))
        sample_headers.append({
            "index": sample_index,
            "offset": f"0x{sample_base:X}",
            "raw_hex": data[sample_base:sample_end].hex(),
        })

    result["header_diagnostics"] = {
        "header_0x00_0x40_hex": data[:0x40].hex(),
        "data_start_valid": 0 <= data_start < len(data),
        "hash_table_offset_valid": (
            extended != 4 or hash_table_offset < len(data)
        ),
        "first_entry_header_samples": sample_headers,
    }

    if depth >= MAX_RECURSION_DEPTH:
        result["recursion_stopped"] = True
        return result

    # La cabecera de cada entry siempre comienza en 0x40 y usa el tamaño
    # indicado en 0x20. El DataOffset es UINT32 para los formatos DS3 de 0x24.
    for index in range(file_count):
        base = 0x40 + index * file_header_size
        try:
            ensure_range(data, base, int(file_header_size), "BND4 file header")

            raw_flags = u8(data, base)
            effective_flags, flag_names = parse_ds3_bnd4_file_flags(raw_flags)

            reserved_bytes = data[base + 1:base + 4].hex()
            reserved_int = s32(data, base + 4, endian)
            compressed_size = u64(data, base + 8, endian)

            cursor = base + 16
            uncompressed_size: int | None = None
            if fmt["has_uncompressed_size"]:
                uncompressed_size = u64(data, cursor, endian)
                cursor += 8

            # CRÍTICO: 32-bit offset en esta variante DS3.
            data_offset = u32(data, cursor, endian)
            cursor += 4

            file_id: int | None = None
            if fmt["has_ids"]:
                file_id = s32(data, cursor, endian)
                cursor += 4

            name_offset = u32(data, cursor, endian)
            cursor += 4

            if name_offset:
                if name_offset >= len(data):
                    name = None
                elif unicode_names:
                    name = read_utf16z(data, name_offset, endian)
                else:
                    name = read_cstring(data, name_offset, "shift_jis")
            else:
                name = None

            payload_end = data_offset + compressed_size
            if compressed_size < 0 or payload_end > len(data):
                # En JSON dejamos el registro de los campos crudos; no
                # interrumpimos el resto del archivo si una entry está corrupta.
                raise ParseError(
                    f"payload fuera de rango: offset=0x{data_offset:X} "
                    f"size=0x{compressed_size:X} file_size=0x{len(data):X}"
                )

            payload = data[data_offset:payload_end]
            before_magic = magic_name(payload)
            after_payload = payload
            decompression_info: dict[str, Any] = {
                "was_compressed": False,
            }

            if raw_flags in DS3_BND4_COMPRESSED_FLAGS:
                after_payload, decompression_info = try_decompress_bnd_entry(
                    payload
                )

            if name and name.lower().endswith(".stayparam"):
                after_magic = "STAYPARAM"
            else:
                after_magic = classify_payload(after_payload)

            entry_record = {
                "index": index,
                "header_offset": f"0x{base:X}",
                "flags_raw": raw_flags,
                "flags_effective": effective_flags,
                "flags_names": flag_names,
                "reserved_bytes": reserved_bytes,
                "reserved_int32": reserved_int,
                "file_id": file_id,
                "name": name,
                "name_offset": f"0x{name_offset:X}" if name_offset else None,
                "compressed_size": compressed_size,
                "uncompressed_size": uncompressed_size,
                "data_offset": f"0x{data_offset:X}",
                "absolute_data_end": f"0x{payload_end:X}",
                "payload_magic_before_decompression": before_magic,
                "payload_magic_after_decompression": after_magic,
                "payload_sha256": safe_sha256(after_payload),
                "preview_hex": (
                    after_payload[:RAW_PREVIEW_BYTES].hex()
                    if INCLUDE_RAW_PREVIEW_HEX else None
                ),
                "compression": decompression_info,
                "source_path": source_path,
            }

            node_label = (
                name
                or (f"id_{file_id}" if file_id is not None else f"entry_{index}")
            )

            if after_magic == "BND4":
                try:
                    child = parse_bnd4(
                        after_payload,
                        f"{source_path}::{node_label}",
                        depth + 1,
                        json_root,
                    )
                    entry_record["child_bnd4"] = child
                except Exception as exc:
                    entry_record["child_error"] = str(exc)

            elif after_magic == "PARAM":
                try:
                    param = parse_param(
                        after_payload,
                        f"{source_path}::{node_label}",
                    )
                    entry_record["child_param"] = param
                except Exception as exc:
                    entry_record["child_error"] = str(exc)

            elif after_magic == "DCX":
                try:
                    dcx_data, dcx_info = decompress_dcx(after_payload)
                    entry_record["child_dcx"] = {
                        "kind": "DCX",
                        "info": dcx_info,
                        "inner_magic": magic_name(dcx_data),
                        "inner_sha256": safe_sha256(dcx_data),
                    }
                    inner_kind = classify_payload(dcx_data)
                    if inner_kind == "BND4":
                        entry_record["child_dcx"]["inner_bnd4"] = parse_bnd4(
                            dcx_data,
                            f"{source_path}::{node_label}::DCX",
                            depth + 1,
                            json_root,
                        )
                    elif inner_kind == "PARAM":
                        entry_record["child_dcx"]["inner_param"] = parse_param(
                            dcx_data,
                            f"{source_path}::{node_label}::DCX",
                        )
                except Exception as exc:
                    entry_record["child_error"] = str(exc)

            result["entries"].append(entry_record)

        except Exception as exc:
            # No abortamos todo el Data0 por una sola entrada inválida. El
            # registro conserva el encabezado y el error para poder corregir
            # el parser con evidencia real.
            result["entries"].append({
                "index": index,
                "header_offset": f"0x{base:X}",
                "parse_error": str(exc),
                "raw_header_hex": data[base:min(len(data), base + int(file_header_size))].hex(),
            })

    return result


# ============================================================================
# PARAM
# ============================================================================

def _decode_scalar(data: bytes, offset: int, kind: str, endian: str) -> Any:
    if offset < 0:
        raise ParseError(f"offset negativo: 0x{offset:X}")
    if kind == "int32":
        return s32(data, offset, endian)
    if kind == "uint32":
        return u32(data, offset, endian)
    if kind == "uint16":
        return u16(data, offset, endian)
    if kind == "uint8":
        return u8(data, offset)
    if kind == "int8":
        value = u8(data, offset)
        return value - 256 if value >= 128 else value
    if kind == "float32":
        raw = bytes(data[offset:offset + 4])
        if len(raw) != 4:
            raise ParseError("float32 fuera de rango")
        fmt = ">f" if endian == "big" else "<f"
        return struct.unpack(fmt, raw)[0]
    raise ParseError(f"tipo de campo no soportado: {kind}")


def _decode_target_fields(
    param_type: str | None,
    data: bytes,
    row_data_offset: int,
    row_size: int,
    endian: str,
) -> dict[str, Any] | None:
    spec = TARGET_PARAM_SPECS.get(param_type or "")
    if not spec:
        return None

    fields: dict[str, Any] = {}
    for field_name, desc in spec["fields"].items():
        rel = int(desc["offset"])
        kind = str(desc["type"])

        if rel >= row_size:
            fields[field_name] = "Null - error"
            continue

        try:
            if kind == "bit":
                raw = u8(data, row_data_offset + rel)
                fields[field_name] = bool(raw & (1 << int(desc["bit"])))
            else:
                width = {
                    "int32": 4,
                    "uint32": 4,
                    "uint16": 2,
                    "uint8": 1,
                    "int8": 1,
                    "float32": 4,
                }[kind]
                if rel + width > row_size:
                    fields[field_name] = "Null - error"
                else:
                    fields[field_name] = _decode_scalar(
                        data, row_data_offset + rel, kind, endian
                    )
        except Exception:
            fields[field_name] = "Null - error"

    return fields


def _validate_param_layout(
    header: dict[str, Any],
    rows: list[dict[str, Any]],
    data: bytes,
    actual_data_start: int | None,
) -> dict[str, Any]:
    param_type = header.get("param_type")
    spec = TARGET_PARAM_SPECS.get(param_type or "")
    detected = header.get("detected_row_size")
    info: dict[str, Any] = {
        "target_param": bool(spec),
        "expected_data_version": spec["data_version"] if spec else None,
        "expected_row_size": spec["row_size"] if spec else None,
        "data_version_matches": (
            spec is None or header.get("paramdef_data_version") == spec["data_version"]
        ),
        "row_size_matches": (
            spec is None or detected == spec["row_size"]
        ),
    }

    pto = header.get("param_type_offset_int")
    if spec and pto is not None and actual_data_start is not None and detected is not None:
        expected_pto = actual_data_start + len(rows) * detected
        info["param_type_offset_expected"] = f"0x{expected_pto:X}"
        info["param_type_offset_matches"] = pto == expected_pto
        info["param_type_offset_int"] = pto
        info["data_start_int"] = actual_data_start
        if pto < len(data):
            actual_type = read_cstring(data, pto, "ascii")
            info["param_type_at_offset"] = actual_type
    return info


def parse_param_header(data: bytes) -> dict[str, Any]:
    if len(data) < 0x40:
        raise ParseError("PARAM demasiado pequeño.")

    byte_order_marker = u8(data, 0x2C)
    if byte_order_marker not in (0, 0xFF):
        raise ParseError(
            f"PARAM endian marker inesperado: 0x{byte_order_marker:02X}"
        )

    endian = "big" if byte_order_marker == 0xFF else "little"

    strings_offset = u32(data, 0x00, endian)
    data_start_short = u16(data, 0x04, endian)
    unk06 = s16(data, 0x06, endian)
    paramdef_data_version = s16(data, 0x08, endian)
    row_count = u16(data, 0x0A, endian)

    format1 = u8(data, 0x2D)
    format2 = u8(data, 0x2E)
    paramdef_format_version = u8(data, 0x2F)

    offset_param_type = bool(format1 & 0x80)
    long_data_offset = bool(format1 & 0x04)
    int_data_offset = bool(format1 & 0x02)
    flag01 = bool(format1 & 0x01)
    unicode_row_names = bool(format2 & 0x01)

    param_type = None
    param_type_offset = None

    if offset_param_type:
        if data[0x0C:0x10] != b"\x00\x00\x00\x00":
            raise ParseError("PARAM OffsetParamType: bloque 0x0C no es cero.")
        param_type_offset = u64(data, 0x10, endian)
        # Candidato inicial. DSMapStudio lo valida otra vez después de
        # calcular RowSize y comprobar ParamTypeOffset.
        if 0 <= param_type_offset < len(data):
            candidate = read_cstring(data, param_type_offset, "ascii")
            if _plausible_param_type(candidate):
                param_type = candidate
    else:
        raw = data[0x0C:0x2C]
        param_type = raw.split(b"\x00", 1)[0].decode(
            "ascii", errors="replace"
        ).strip()

    # DSMapStudio lee los flags en 0x2C, después salta el campo Format (0x30
    # en la estructura física), y solamente entonces consume DataStart. En
    # los formatos expandidos el bloque ocupa 0x30..0x3F; por eso los row
    # headers comienzan en 0x40.
    if flag01 and int_data_offset:
        data_start = u32(data, 0x30, endian)
        trailing = data[0x34:0x40]
        data_start_format = "int32"
        data_start_header_size = 16
        if trailing != b"\x00" * 12:
            data_start_padding_valid = False
        else:
            data_start_padding_valid = True
        row_header_size = 24 if long_data_offset else 24 if False else 12
    elif long_data_offset:
        data_start = u64(data, 0x30, endian)
        trailing = data[0x38:0x40]
        data_start_format = "int64"
        data_start_header_size = 16
        data_start_padding_valid = trailing == b"\x00" * 8
        row_header_size = 24
    else:
        data_start = data_start_short
        data_start_format = "uint16"
        data_start_header_size = 4
        data_start_padding_valid = None
        row_header_size = 12

    if offset_param_type and param_type_offset is not None:
        # El tipo se valida después; aquí solamente guardamos el offset.
        param_type_offset_display = f"0x{param_type_offset:X}"
    else:
        param_type_offset_display = None

    return {
        "kind": "PARAM",
        "size_bytes": len(data),
        "sha256": safe_sha256(data),
        "param_type": param_type,
        "param_type_offset": param_type_offset_display,
        "param_type_offset_int": param_type_offset,
        "endian": endian,
        "big_endian": endian == "big",
        "strings_offset": f"0x{strings_offset:X}",
        "strings_offset_int": strings_offset,
        "data_start": f"0x{data_start:X}",
        "data_start_int": data_start,
        "data_start_format": data_start_format,
        "data_start_header_size": data_start_header_size,
        "data_start_padding_valid": data_start_padding_valid,
        "unk06": unk06,
        "paramdef_data_version": paramdef_data_version,
        "paramdef_format_version": paramdef_format_version,
        "row_count": row_count,
        "format1_raw": format1,
        "format1_flags": {
            "Flag01": bool(format1 & 0x01),
            "IntDataOffset": bool(format1 & 0x02),
            "LongDataOffset": bool(format1 & 0x04),
            "Flag08": bool(format1 & 0x08),
            "Flag10": bool(format1 & 0x10),
            "Flag20": bool(format1 & 0x20),
            "Flag40": bool(format1 & 0x40),
            "OffsetParamType": bool(format1 & 0x80),
        },
        "format2_raw": format2,
        "format2_flags": {
            "UnicodeRowNames": unicode_row_names,
            "Flag02": bool(format2 & 0x02),
            "Flag04": bool(format2 & 0x04),
            "Flag08": bool(format2 & 0x08),
            "Flag10": bool(format2 & 0x10),
            "Flag20": bool(format2 & 0x20),
            "Flag40": bool(format2 & 0x40),
            "Flag80": bool(format2 & 0x80),
        },
        "row_header_size": row_header_size,
    }


def parse_param(data: bytes, source_path: str) -> dict[str, Any]:
    header = parse_param_header(data)

    endian = header["endian"]
    long_data_offset = header["format1_flags"]["LongDataOffset"]
    int_data_offset = header["format1_flags"]["IntDataOffset"]
    flag01 = header["format1_flags"]["Flag01"]
    row_count = header["row_count"]

    # DSMapStudio: expanded PARAM variants consume the complete 0x30..0x3F
    # block before reading row headers; compact variants start at 0x30.
    expanded_variant = (
        (flag01 and int_data_offset) or long_data_offset
    )
    rows_start = 0x40 if expanded_variant else 0x30

    row_header_size = header["row_header_size"]
    ensure_range(
        data,
        rows_start,
        row_count * row_header_size,
        "PARAM row headers",
    )

    rows: list[dict[str, Any]] = []
    actual_strings_offset: int | None = None
    previous_data_offset: int | None = None
    detected_row_size: int | None = None
    data_offsets: list[int] = []

    for i in range(row_count):
        off = rows_start + i * row_header_size

        row_id = s32(data, off, endian)

        if long_data_offset:
            unused = s32(data, off + 4, endian)
            data_offset = u64(data, off + 8, endian)
            name_offset = u64(data, off + 16, endian)
        else:
            unused = None
            data_offset = u32(data, off + 4, endian)
            name_offset = u32(data, off + 8, endian)

        data_offsets.append(data_offset)

        row_name = None
        if name_offset:
            if name_offset >= len(data):
                row_name = "Null - error"
            else:
                if actual_strings_offset is None:
                    actual_strings_offset = name_offset
                else:
                    actual_strings_offset = min(actual_strings_offset, name_offset)

                if INCLUDE_ROW_NAMES:
                    if header["format2_flags"]["UnicodeRowNames"]:
                        row_name = read_utf16z(data, name_offset, endian)
                    else:
                        row_name = read_cstring(data, name_offset, "shift_jis")

        rows.append({
            "index": i,
            "id": row_id,
            "name": row_name,
            "data_offset": f"0x{data_offset:X}",
            "data_offset_int": data_offset,
            "name_offset": f"0x{name_offset:X}" if name_offset else None,
            "name_offset_int": name_offset if name_offset else None,
            "unused_or_padding": unused,
        })

        if previous_data_offset is not None:
            delta = data_offset - previous_data_offset
            if delta > 0 and detected_row_size is None:
                detected_row_size = delta
        previous_data_offset = data_offset

    if len(rows) > 1:
        deltas = [
            data_offsets[i + 1] - data_offsets[i]
            for i in range(len(data_offsets) - 1)
        ]
        positive = [d for d in deltas if d > 0]
        detected_row_size = positive[0] if positive else None
        header["row_size_deltas_unique"] = sorted(set(deltas))[:64]
        header["row_size_deltas_all_positive"] = (
            len(positive) == len(deltas) and len(deltas) > 0
        )
    elif len(rows) == 1:
        string_base = (
            actual_strings_offset
            if actual_strings_offset is not None
            else header["strings_offset_int"]
        )
        detected_row_size = string_base - data_offsets[0]
    else:
        detected_row_size = 0

    if detected_row_size is not None and detected_row_size <= 0:
        detected_row_size = None

    actual_data_start = min(data_offsets) if data_offsets else None

    # Misma comprobación que hace DSMapStudio:
    # ParamTypeOffset == DataStart + RowCount * RowSize.
    param_type_validation = None
    if header["format1_flags"]["OffsetParamType"]:
        pto = header.get("param_type_offset_int")
        if (
            pto is not None
            and actual_data_start is not None
            and detected_row_size is not None
        ):
            expected = actual_data_start + row_count * detected_row_size
            param_type_validation = {
                "offset": f"0x{pto:X}",
                "expected_offset": f"0x{expected:X}",
                "offset_matches": pto == expected,
            }
            if pto == expected and pto < len(data):
                actual_type = read_cstring(data, pto, "ascii")
                if _plausible_param_type(actual_type):
                    header["param_type"] = actual_type
                    param_type_validation["value"] = actual_type
                    param_type_validation["ascii_valid"] = True
                else:
                    header["param_type"] = None
                    param_type_validation["value"] = actual_type
                    param_type_validation["ascii_valid"] = False

    if detected_row_size is not None:
        for row in rows:
            start = row["data_offset_int"]
            end = start + detected_row_size

            if start < 0 or end > len(data) or end < start:
                row["data_bounds"] = "Null - error"
                continue

            row["data_end"] = f"0x{end:X}"
            row["data_size"] = detected_row_size

            if INCLUDE_ROW_DATA_PREVIEW:
                row["data_preview_hex"] = data[
                    start:min(end, start + ROW_DATA_PREVIEW_BYTES)
                ].hex()

            fields = _decode_target_fields(
                header["param_type"],
                data,
                start,
                detected_row_size,
                endian,
            )
            if fields is not None:
                row["fields"] = fields

    # Validación central basada en la misma regla usada por DSMapStudio:
    # RowSize = DataIndex[1] - DataIndex[0], y ParamTypeOffset debe coincidir
    # con dataStart + rowCount * RowSize cuando OffsetParamType está activo.
    # Guardar RowSize antes de validar, igual que DSMapStudio lo establece
    # después de leer los Row headers y antes de comprobar ParamTypeOffset.
    header["detected_row_size"] = detected_row_size
    validation = _validate_param_layout(
        header,
        rows,
        data,
        actual_data_start,
    )
    if param_type_validation is not None:
        validation["param_type_validation"] = param_type_validation

    header["detected_row_size"] = detected_row_size
    header["actual_data_start"] = (
        f"0x{actual_data_start:X}" if actual_data_start is not None else None
    )
    header["row_headers_start"] = f"0x{rows_start:X}"
    header["validation"] = validation

    # Índice directo para el futuro editor. IDs no siempre son únicos en
    # todos los juegos, por eso el valor es una lista de índices.
    row_id_index: dict[str, list[int]] = {}
    for row in rows:
        row_id_index.setdefault(str(row["id"]), []).append(row["index"])

    for row in rows:
        row.pop("data_offset_int", None)
        row.pop("name_offset_int", None)

    return {
        "kind": "PARAM",
        "source_path": source_path,
        "header": header,
        "row_id_index": row_id_index,
        "rows": rows,
    }


# ============================================================================
# RUTAS CALCULADAS
# ============================================================================

def configure_paths(game_root: str | None = None, mod_directory: str | None = None) -> engine_config.EnginePaths:
    global GAME_DIRECTORY_PATH, DATA0_PATH, DATA0_BHD_PATH
    global MOD_DIRECTORY_PATH, OUTPUT_DIRECTORY, OUTPUT_PATH

    paths = engine_config.get_paths(
        game_root=game_root,
        mod_directory=mod_directory,
        script_dir=Path(__file__).resolve().parent,
    )
    GAME_DIRECTORY_PATH = paths.game_root
    DATA0_PATH = paths.data0_bdt
    DATA0_BHD_PATH = paths.data0_bhd
    MOD_DIRECTORY_PATH = paths.mod_directory
    OUTPUT_DIRECTORY = paths.enemy_tables
    OUTPUT_PATH = paths.data0_json
    return paths


# ============================================================================
# BUSCADOR PRINCIPAL: SOLO GAME\Data0.bdt
# ============================================================================

def validate_config() -> None:
    if not GAME_DIRECTORY_PATH.exists():
        raise ParseError(
            f"No existe GAME_DIRECTORY: {GAME_DIRECTORY_PATH}"
        )

    if not DATA0_PATH.is_file():
        raise ParseError(
            f"No existe Data0.bdt en la raíz de Game: {DATA0_PATH}"
        )

    if engine_config.REQUIRE_DATA0_BHD and not DATA0_BHD_PATH.is_file():
        raise ParseError(
            f"REQUIRE_DATA0_BHD está activo pero no existe Data0.bhd: {DATA0_BHD_PATH}"
        )

    if STRICT_GAME_DATA0_ONLY:
        expected_parent = GAME_DIRECTORY_PATH.resolve()
        actual_parent = DATA0_PATH.parent.resolve()

        if actual_parent != expected_parent:
            raise ParseError(
                "STRICT_GAME_DATA0_ONLY rechazó el Data0 fuera de Game."
            )


def build_data0_json() -> dict[str, Any]:
    validate_config()

    raw = DATA0_PATH.read_bytes()
    raw_sha256 = safe_sha256(raw)

    print(f"[DATA0] {DATA0_PATH}")
    print(f"[DATA0] Size: {len(raw):,} bytes")
    print(f"[DATA0] Raw SHA256: {raw_sha256}")
    print(f"[DATA0] Raw first 32 bytes: {raw[:32].hex()}")

    decrypted, crypto_info = decrypt_data0(raw)

    print(f"[AES] decrypted size: {len(decrypted):,} bytes")
    print(f"[AES] decrypted magic: {decrypted[:8]!r}")

    decrypted_info: dict[str, Any] = {
        "size_bytes": len(decrypted),
        "sha256": safe_sha256(decrypted),
        "first_32_bytes_hex": decrypted[:32].hex(),
        "magic": magic_name(decrypted),
    }

    # SoulsFormats trata DS3 Data0 como una regulación que, después de
    # descifrar, se puede leer como BND4. También aceptamos DCX como
    # fallback diagnóstico para variantes o herramientas que lo empaqueten.
    normalized = decrypted
    outer_dcx = None

    if normalized.startswith(DCX_MAGIC):
        normalized, outer_dcx = decompress_dcx(normalized)
        print(
            f"[DCX] outer decompressed size: "
            f"{len(normalized):,} bytes"
        )
        print(f"[DCX] inner magic: {normalized[:8]!r}")

    root_kind = classify_payload(normalized)
    print(f"[ROOT] detected: {root_kind}")

    if root_kind == "BND4" and len(normalized) >= 0x40:
        try:
            marker = u32(normalized, 0x08, "little")
            bnd_endian = "little" if marker == 0x00010000 else "big" if marker == 0x00000100 else "UNKNOWN"
            print(f"[BND4] endian marker 0x08: 0x{marker:08X} -> {bnd_endian}")
            if bnd_endian != "UNKNOWN":
                print(f"[BND4] fileCount: {u32(normalized, 0x0C, bnd_endian)}")
                print(f"[BND4] headerSize: 0x{u64(normalized, 0x10, bnd_endian):X}")
                print(f"[BND4] fileHeaderSize: 0x{u64(normalized, 0x20, bnd_endian):X}")
                print(f"[BND4] dataStart: 0x{u64(normalized, 0x28, bnd_endian):X}")
                print(f"[BND4] Unicode: {bool(u8(normalized, 0x30))}")
                raw_fmt = u8(normalized, 0x31)
                control2 = u8(normalized, 0x0A)
                bit_big = not bool(control2)
                eff_fmt = raw_fmt if bit_big else reverse_bits_byte(raw_fmt)
                print(
                    f"[BND4] Format raw: 0x{raw_fmt:02X}; "
                    f"effective: 0x{eff_fmt:02X}; bitBigEndian={bit_big}"
                )
                print(f"[BND4] Extended: 0x{u8(normalized, 0x32):02X}")
        except Exception as exc:
            print(f"[BND4] header diagnostics failed: {exc}")

    result: dict[str, Any] = {
        "tool": {
            "name": "DS3 Data0 Reader",
            "version": "4.0-ds3-dsmapstudio-param-layout",
            "mode": "read_only",
            "external_libraries": False,
        },
        "source": {
            "game_directory": str(GAME_DIRECTORY_PATH),
            "data0_path": str(DATA0_PATH),
            "data0_filename": DATA0_PATH.name,
            "strict_game_data0_only": STRICT_GAME_DATA0_ONLY,
            "file_size_bytes": len(raw),
            "sha256": raw_sha256,
            "data0_bhd": {
                "path": str(DATA0_BHD_PATH),
                "filename": DATA0_BHD_PATH.name,
                "exists": DATA0_BHD_PATH.is_file(),
                "file_size_bytes": DATA0_BHD_PATH.stat().st_size if DATA0_BHD_PATH.is_file() else None,
                "sha256": engine_config.sha256_file(DATA0_BHD_PATH) if DATA0_BHD_PATH.is_file() else None,
            },
        },
        "crypto": crypto_info,
        "decrypted": decrypted_info,
        "outer_dcx": outer_dcx,
        "root_kind": root_kind,
        "bnd4": None,
        "top_level_identifiers": [],
        "summary": {},
    }

    if root_kind == "BND4":
        bnd = parse_bnd4(
            normalized,
            str(DATA0_PATH),
            depth=0,
            json_root=result,
        )
        result["bnd4"] = bnd

        # Generamos índices cómodos para el futuro editor.
        all_entries = []

        def walk_bnd(node: dict[str, Any], parent_path: str) -> None:
            for entry in node.get("entries", []):
                entry_name = entry.get("name")
                entry_id = entry.get("file_id")
                entry_path = (
                    f"{parent_path}::{entry_name}"
                    if entry_name
                    else f"{parent_path}::entry_{entry['index']}"
                )

                identifier = {
                    "path": entry_path,
                    "index": entry["index"],
                    "file_id": entry_id,
                    "name": entry_name,
                    "header_offset": entry["header_offset"],
                    "data_offset": entry["data_offset"],
                    "compressed_size": entry["compressed_size"],
                    "uncompressed_size": entry["uncompressed_size"],
                    "flags_effective": entry["flags_effective"],
                    "flags_names": entry["flags_names"],
                    "payload_kind": entry["payload_magic_after_decompression"],
                }

                all_entries.append(identifier)

                child = entry.get("child_bnd4")
                if isinstance(child, dict):
                    walk_bnd(child, entry_path)

                child_dcx = entry.get("child_dcx")
                if isinstance(child_dcx, dict):
                    inner_bnd = child_dcx.get("inner_bnd4")
                    if isinstance(inner_bnd, dict):
                        walk_bnd(inner_bnd, entry_path + "::DCX")

                child_param = entry.get("child_param")
                if isinstance(child_param, dict):
                    phead = child_param.get("header", {})
                    ptype = phead.get("param_type")
                    if ptype:
                        result["top_level_identifiers"].append({
                            "type": "PARAM",
                            "param_type": ptype,
                            "row_count": phead.get("row_count"),
                            "path": entry_path,
                            "file_id": entry_id,
                            "name": entry_name,
                        })

        walk_bnd(
            bnd,
            str(DATA0_PATH),
        )

        # Buscar PARAMs dentro de los registros sin depender de nombres.
        def walk_for_params(node: Any, current_path: str) -> None:
            if isinstance(node, dict):
                if node.get("kind") == "PARAM":
                    header = node.get("header", {})
                    ptype = header.get("param_type")
                    if ptype:
                        marker = {
                            "type": "PARAM",
                            "param_type": ptype,
                            "row_count": header.get("row_count"),
                            "row_size": header.get("detected_row_size"),
                            "path": current_path,
                        }
                        if marker not in result["top_level_identifiers"]:
                            result["top_level_identifiers"].append(marker)
                for key, value in node.items():
                    if key in {"entries", "child_bnd4", "child_param", "child_dcx", "inner_bnd4", "inner_param"}:
                        walk_for_params(value, f"{current_path}::{key}")
            elif isinstance(node, list):
                for idx, value in enumerate(node):
                    walk_for_params(value, f"{current_path}[{idx}]")

        walk_for_params(
            bnd,
            str(DATA0_PATH),
        )

        # Resumen.
        param_types = []
        row_total = 0
        nested_bnds = 0

        def summarize(node: Any) -> None:
            nonlocal row_total, nested_bnds
            if isinstance(node, dict):
                if node.get("kind") == "PARAM":
                    h = node.get("header", {})
                    ptype = h.get("param_type")
                    if ptype and ptype not in param_types:
                        param_types.append(ptype)
                    row_total += h.get("row_count", 0) or 0
                if node.get("kind") == "BND4":
                    nested_bnds += 1
                for value in node.values():
                    summarize(value)
            elif isinstance(node, list):
                for value in node:
                    summarize(value)

        summarize(bnd)

        result["summary"] = {
            "root_bnd4_file_count": bnd["header"]["file_count"],
            "root_bnd4_format_raw": bnd["header"]["format_raw"],
            "root_bnd4_format_effective": bnd["header"].get("format_effective"),
            "root_bnd4_format_flags": bnd["header"].get("format_flags", []),
            "entries_indexed": len(all_entries),
            "nested_bnd4_containers": nested_bnds,
            "unique_param_types": len(param_types),
            "total_param_rows_seen": row_total,
            "param_types": sorted(param_types),
        }

        result["all_entries_index"] = all_entries

    elif root_kind == "PARAM":
        result["summary"] = {
            "root_is_param": True,
        }
        result["root_param"] = parse_param(
            normalized,
            str(DATA0_PATH),
        )
    else:
        result["summary"] = {
            "root_is_bnd4": False,
            "error": (
                "Tras descifrar Data0 no apareció BND4 ni una estructura PARAM "
                f"reconocible. Magic={normalized[:16]!r}"
            ),
        }

    return result


# ============================================================================
# MAIN
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lee únicamente Game\\Data0.bdt de Dark Souls III y genera data0_original.json."
    )
    parser.add_argument(
        "--game-root",
        default=None,
        help="Ruta a DARK SOULS III\\Game. Por defecto: padre de PythonEngineDS3.",
    )
    parser.add_argument(
        "--mod-directory",
        default=None,
        help="Ruta absoluta o relativa a Game de la carpeta del mod.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        configure_paths(args.game_root, args.mod_directory)

        print("=" * 78)
        print("DS3 Data0 Reader v4 — DS3 BND4 + DSMapStudio PARAM layout")
        print("=" * 78)
        print(f"Game:       {GAME_DIRECTORY_PATH}")
        print(f"Data0.bdt:  {DATA0_PATH}")
        print(f"Data0.bhd:  {DATA0_BHD_PATH} ({'OK' if DATA0_BHD_PATH.is_file() else 'NO ENCONTRADO'})")
        print(f"Mod:        {MOD_DIRECTORY_PATH}")
        print(f"JSON:       {OUTPUT_PATH}")
        print("Modo:       READ ONLY")
        print()

        result = build_data0_json()

        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=4,
            ),
            encoding="utf-8",
        )

        print()
        print("=" * 78)
        print("FINALIZADO")
        print("=" * 78)
        print(f"JSON: {OUTPUT_PATH}")

        summary = result.get("summary", {})
        print(f"Root kind: {result.get('root_kind')}")
        print(f"Entries indexed: {summary.get('entries_indexed', 0)}")
        print(f"Unique PARAM types: {summary.get('unique_param_types', 0)}")

        def find_param_node(node: Any, wanted: str) -> dict[str, Any] | None:
            if isinstance(node, dict):
                if node.get("kind") == "PARAM":
                    if node.get("header", {}).get("param_type") == wanted:
                        return node
                for value in node.values():
                    found = find_param_node(value, wanted)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_param_node(value, wanted)
                    if found is not None:
                        return found
            return None

        print()
        print("VALIDACION DE PARAM CRITICOS:")
        for ptype, row_id in [
            ("NPC_PARAM_ST", 27920),
            ("NPC_THINK_PARAM_ST", 27920),
            ("ITEMLOT_PARAM_ST", 10),
        ]:
            node = find_param_node(result.get("bnd4"), ptype)
            if node is None:
                print(f"  - {ptype}: NO IDENTIFICADO")
                continue
            h = node.get("header", {})
            v = h.get("validation", {})
            print(
                f"  - {ptype}: rows={h.get('row_count')} "
                f"row_size={h.get('detected_row_size')} "
                f"layout_ok={v.get('row_size_matches', True)}"
            )
            matches = [row for row in node.get("rows", []) if row.get("id") == row_id]
            if matches:
                row = matches[0]
                print(f"      ID {row_id}: data={row.get('data_offset')} fields={row.get('fields', {})}")
            else:
                print(f"      ID {row_id}: NO ENCONTRADO")

        params = result.get("top_level_identifiers", [])
        if params:
            print()
            print("PARAM identificados:")
            seen = set()
            for item in params:
                ptype = item.get("param_type")
                if ptype and ptype not in seen:
                    print(
                        f"  - {ptype} "
                        f"(rows={item.get('row_count')}, "
                        f"path={item.get('path')})"
                    )
                    seen.add(ptype)

        return 0

    except Exception as exc:
        print()
        print("=" * 78)
        print("FATAL ERROR")
        print("=" * 78)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
