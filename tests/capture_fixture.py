"""Construct a harmless x64 PE fixture for native capture integration tests.

The generated DLL has no imports and no filesystem, network, process or IPC
behavior.  Its only callable function returns 1 after a RIP-relative LEA of a
public SHA-512 constants table.  All input data is synthetic and fixed here.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import sys


FIXTURE_KEY = bytes((index * 7 + 11) % 256 for index in range(32))
BLOCK_ENTRY_RVA = 0x1040
TABLE_RVA = 0x2000
IMAGE_SIZE = 0x6000

# Public FIPS 180-4 SHA-512 round constants, stored as little-endian uint64s.
SHA512_K = (
    0x428A2F98D728AE22, 0x7137449123EF65CD, 0xB5C0FBCFEC4D3B2F,
    0xE9B5DBA58189DBBC, 0x3956C25BF348B538, 0x59F111F1B605D019,
    0x923F82A4AF194F9B, 0xAB1C5ED5DA6D8118, 0xD807AA98A3030242,
    0x12835B0145706FBE, 0x243185BE4EE4B28C, 0x550C7DC3D5FFB4E2,
    0x72BE5D74F27B896F, 0x80DEB1FE3B1696B1, 0x9BDC06A725C71235,
    0xC19BF174CF692694, 0xE49B69C19EF14AD2, 0xEFBE4786384F25E3,
    0x0FC19DC68B8CD5B5, 0x240CA1CC77AC9C65, 0x2DE92C6F592B0275,
    0x4A7484AA6EA6E483, 0x5CB0A9DCBD41FBD4, 0x76F988DA831153B5,
    0x983E5152EE66DFAB, 0xA831C66D2DB43210, 0xB00327C898FB213F,
    0xBF597FC7BEEF0EE4, 0xC6E00BF33DA88FC2, 0xD5A79147930AA725,
    0x06CA6351E003826F, 0x142929670A0E6E70, 0x27B70A8546D22FFC,
    0x2E1B21385C26C926, 0x4D2C6DFC5AC42AED, 0x53380D139D95B3DF,
    0x650A73548BAF63DE, 0x766A0ABB3C77B2A8, 0x81C2C92E47EDAEE6,
    0x92722C851482353B, 0xA2BFE8A14CF10364, 0xA81A664BBC423001,
    0xC24B8B70D0F89791, 0xC76C51A30654BE30, 0xD192E819D6EF5218,
    0xD69906245565A910, 0xF40E35855771202A, 0x106AA07032BBD1B8,
    0x19A4C116B8D2D0C8, 0x1E376C085141AB53, 0x2748774CDF8EEB99,
    0x34B0BCB5E19B48A8, 0x391C0CB3C5C95A63, 0x4ED8AA4AE3418ACB,
    0x5B9CCA4F7763E373, 0x682E6FF3D6B2B8A3, 0x748F82EE5DEFB2FC,
    0x78A5636F43172F60, 0x84C87814A1F0AB72, 0x8CC702081A6439EC,
    0x90BEFFFA23631E28, 0xA4506CEBDE82BDE9, 0xBEF9A3F7B2C67915,
    0xC67178F2E372532B, 0xCA273ECEEA26619C, 0xD186B8C721C0C207,
    0xEADA7DD6CDE0EB1E, 0xF57D4F7FEE6ED178, 0x06F067AA72176FBA,
    0x0A637DC5A2C898A6, 0x113F9804BEF90DAE, 0x1B710B35131C471B,
    0x28DB77F523047D84, 0x32CAAB7B40C72493, 0x3C9EBE0A15C9BEBC,
    0x431D67C49C100D4C, 0x4CC5D4BECB3E42B6, 0x597F299CFC657E2A,
    0x5FCB6FAB3AD6FAEC, 0x6C44198C4A475817,
)
SHA512_TABLE = struct.pack('<80Q', *SHA512_K)


def build_fixture(path: Path) -> Path:
    """Write a deterministic, import-free PE32+ DLL with real unwind metadata."""
    text = bytearray(b'\xCC' * 0x200)
    # BOOL WINAPI DllMain(HINSTANCE, DWORD, LPVOID) { return TRUE; }
    text[:6] = b'\xB8\x01\x00\x00\x00\xC3'
    # int CaptureBlock(void *context, const void *input128, size_t count).
    # The long valid NOP prefix is deliberate: Frida can relocate its prologue.
    block = b'\x90' * 32
    lea_next = BLOCK_ENTRY_RVA + len(block) + 7
    block += b'\x48\x8D\x05' + struct.pack('<i', TABLE_RVA - lea_next)
    block += b'\xB8\x01\x00\x00\x00\xC3'
    text[0x40:0x40 + len(block)] = block

    rdata = bytearray(0x400)
    rdata[:len(SHA512_TABLE)] = SHA512_TABLE
    # Export directory and strings live after the constants, all RVAs are local.
    struct.pack_into('<IIHHIIIIIII', rdata, 0x300,
                     0, 0, 0, 0, 0x2360, 1, 1, 1, 0x2340, 0x2344, 0x2348)
    struct.pack_into('<IIH', rdata, 0x340, BLOCK_ENTRY_RVA, 0x2380, 0)
    rdata[0x360:0x360 + len(b'CaptureFixture.dll\0')] = b'CaptureFixture.dll\0'
    rdata[0x380:0x380 + len(b'CaptureBlock\0')] = b'CaptureBlock\0'

    pdata = bytearray(0x200)
    struct.pack_into('<III', pdata, 0, 0x1000, 0x1006, 0x4000)
    struct.pack_into('<III', pdata, 12, BLOCK_ENTRY_RVA,
                     BLOCK_ENTRY_RVA + len(block), 0x4004)
    xdata = bytearray(0x200)
    # Version 1, no prologue stack changes, no unwind codes or handlers.
    xdata[:8] = b'\x01\x00\x00\x00' * 2
    reloc = bytearray(0x200)
    # An empty ABSOLUTE block permits relocation without any absolute addresses.
    struct.pack_into('<IIHH', reloc, 0, 0x1000, 12, 0, 0)

    sections = [
        (b'.text', 0x1000, 0x400, text, 0x60000020),
        (b'.rdata', 0x2000, 0x600, rdata, 0x40000040),
        (b'.pdata', 0x3000, 0xA00, pdata, 0x40000040),
        (b'.xdata', 0x4000, 0xC00, xdata, 0x40000040),
        (b'.reloc', 0x5000, 0xE00, reloc, 0x42000040),
    ]
    result = bytearray(0x1000)
    result[:2] = b'MZ'
    struct.pack_into('<I', result, 0x3C, 0x80)
    result[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', result, 0x84, 0x8664, len(sections),
                     0, 0, 0, 0xF0, 0x2022)
    optional = 0x98
    struct.pack_into('<HBBIIIII', result, optional, 0x20B, 14, 0,
                     0x200, 0xA00, 0, 0x1000, 0x1000)
    struct.pack_into('<QII', result, optional + 24,
                     0x180000000, 0x1000, 0x200)
    struct.pack_into('<HHHHHH', result, optional + 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into('<IIII', result, optional + 52, 0, IMAGE_SIZE, 0x400, 0)
    struct.pack_into('<HH', result, optional + 68, 3, 0x160)
    struct.pack_into('<QQQQII', result, optional + 72,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    for index, rva, size in [(0, 0x2300, 0x100), (3, 0x3000, 24),
                             (5, 0x5000, 12)]:
        struct.pack_into('<II', result, optional + 112 + index * 8, rva, size)
    for index, (name, rva, raw_offset, content, flags) in enumerate(sections):
        header = optional + 0xF0 + index * 40
        struct.pack_into('<8sIIIIIIHHI', result, header, name, len(content), rva,
                         len(content), raw_offset, 0, 0, 0, 0, flags)
        result[raw_offset:raw_offset + len(content)] = content
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result)
    return path


def _reply(**payload):
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def child(path: Path) -> int:
    """A stdin-controlled process; only invokes our explicitly passed fixture."""
    path = Path(path).resolve(strict=True)
    expected = path.with_name('_fixture_reference.dll')
    build_fixture(expected)
    try:
        if path.read_bytes() != expected.read_bytes():
            raise ValueError('fixture content mismatch')
    finally:
        expected.unlink(missing_ok=True)
    dll = ctypes.WinDLL(str(path))
    function = dll.CaptureBlock
    function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    function.restype = ctypes.c_int
    context = ctypes.create_string_buffer(256)
    _reply(kind='loaded', pid=os.getpid(), module=str(path),
           sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    for line in sys.stdin:
        request = json.loads(line)
        command = request.get('command')
        if command == 'stop':
            _reply(kind='stopped')
            return 0
        if command in ('valid', 'invalid', 'probe'):
            pad = bytes(value ^ 0x36 for value in FIXTURE_KEY) + b'\x36' * 96
            if command != 'valid':
                pad = pad[:-1] + b'\x35'
            buffer = ctypes.create_string_buffer(pad, 128)
            returned = function(context, buffer, 1)
            response = {'kind': 'returned', 'command': command, 'result': returned}
            if command == 'probe':
                response['entry_pristine'] = (
                    ctypes.string_at(ctypes.cast(function, ctypes.c_void_p), 32)
                    == b'\x90' * 32
                )
            _reply(**response)
        else:
            _reply(kind='invalid_command')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=Path)
    options = parser.parse_args()
    if not options.child:
        parser.error('the fixture only runs as an explicitly requested child')
    raise SystemExit(child(options.child))
