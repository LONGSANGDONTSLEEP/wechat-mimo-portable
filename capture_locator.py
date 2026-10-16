"""Read-only discovery of SHA-512 entry points in an AMD64 PE image.

The constant-table / RIP-relative-reference approach was informed by
https://github.com/freanocank/wechat-decrypt/blob/main/scripts/windows/extract_raw_key.py
(MIT, Copyright (c) 2026 cocofeng).  This implementation uses the PE exception
directory for function boundaries; it never searches backwards for padding and
does not attach to, start, stop, or modify any process.

Upstream permission notice: Permission is hereby granted, free of charge, to any
person obtaining a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including without
limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions: The above copyright
notice and this permission notice shall be included in all copies or substantial
portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct
from typing import Iterable, Sequence


IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_READ = 0x40000000
IMAGE_SCN_MEM_WRITE = 0x80000000
MAX_FUNCTION_SIZE = 128 * 1024
# Eight independently distinctive SHA-512 round constants. OpenSSL's assembler
# may repeat each pair for SIMD lanes; a match in the second copy of its first
# pair must not be mistaken for a scalar table start.
SHA512_TABLE_PREFIX = struct.pack(
    "<QQQQQQQQ", 0x428A2F98D728AE22, 0x7137449123EF65CD,
    0xB5C0FBCFEC4D3B2F, 0xE9B5DBA58189DBBC,
    0x3956C25BF348B538, 0x59F111F1B605D019,
    0x923F82A4AF194F9B, 0xAB1C5ED5DA6D8118,
)
_TABLE_LAYOUTS = (SHA512_TABLE_PREFIX,) + tuple(
    b"".join(SHA512_TABLE_PREFIX[offset:offset + 16] * lanes for offset in range(0, 64, 16))
    for lanes in (2, 4)
)
# REX.W plus optional REX.R, LEA, RIP-relative ModRM with any destination.
_RIP_LEA = re.compile(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....", re.DOTALL)


@dataclass(frozen=True)
class ImageSection:
    name: str
    rva: int
    data: bytes
    characteristics: int

    @property
    def end(self) -> int:
        return self.rva + len(self.data)

    def contains(self, rva: int, length: int = 1) -> bool:
        return length >= 0 and self.rva <= rva and rva + length <= self.end


@dataclass(frozen=True, order=True)
class RuntimeFunction:
    begin: int
    end: int
    unwind_rva: int


def find_tables(sections: Sequence[ImageSection]) -> list[int]:
    """Accept initialized readable, non-writable data, including assembler .text."""
    found: set[int] = set()
    for section in sections:
        if not section.characteristics & IMAGE_SCN_MEM_READ:
            continue
        if section.characteristics & IMAGE_SCN_MEM_WRITE:
            continue
        for pattern in _TABLE_LAYOUTS:
            start = 0
            while True:
                offset = section.data.find(pattern, start)
                if offset < 0:
                    break
                found.add(section.rva + offset)
                start = offset + 1
    return sorted(found)


def _section_at(sections: Sequence[ImageSection], rva: int, length: int = 1) -> ImageSection | None:
    return next((section for section in sections if section.contains(rva, length)), None)


def _function_reason(function: RuntimeFunction, sections: Sequence[ImageSection], xref: int) -> str | None:
    length = function.end - function.begin
    if length < 8 or length > MAX_FUNCTION_SIZE:
        return "函数边界长度异常"
    code = _section_at(sections, function.begin, length)
    if code is None or not code.characteristics & IMAGE_SCN_MEM_EXECUTE:
        return "函数边界超出可执行节"
    if not function.begin <= xref or xref + 7 > function.end:
        return "引用不在完整函数边界内"
    unwind = _section_at(sections, function.unwind_rva, 4)
    if unwind is None or not unwind.characteristics & IMAGE_SCN_MEM_READ:
        return "异常展开信息不可读"
    offset = function.unwind_rva - unwind.rva
    header = unwind.data[offset:offset + 4]
    # AMD64 UNWIND_INFO versions 1/2. A leaf with zero unwind codes is valid.
    version, flags = header[0] & 7, header[0] >> 3
    if version not in (1, 2) or flags > 7 or (flags & 4 and flags & 3):
        return "异常展开信息格式不受支持"
    if header[1] > length or not unwind.contains(function.unwind_rva, 4 + header[2] * 2):
        return "异常展开信息不完整"
    return None


def locate_entries(
    sections: Sequence[ImageSection], functions: Iterable[RuntimeFunction],
) -> tuple[list[int], list[int], list[dict]]:
    """Resolve real LEA references to exception-directory-backed function starts.

    Diagnostics deliberately distinguish missing xrefs, missing function bounds,
    and invalid unwind metadata. No address is accepted solely by a byte pattern.
    """
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    from capstone.x86 import X86_INS_LEA, X86_OP_MEM, X86_REG_RIP

    tables = find_tables(sections)
    diagnostics = {rva: {"table_rva": rva, "entry_rvas": [], "rejected": []} for rva in tables}
    if not tables:
        return [], [], []
    table_set = set(tables)
    bounds = sorted(set(functions))
    starts = [item.begin for item in bounds]
    prefix_ends: list[int] = []
    highest = 0
    for item in bounds:
        highest = max(highest, item.end)
        prefix_ends.append(highest)
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    entries: set[int] = set()
    for section in sections:
        if not section.characteristics & IMAGE_SCN_MEM_EXECUTE:
            continue
        for match in _RIP_LEA.finditer(section.data):
            offset = match.start()
            xref = section.rva + offset
            target = xref + 7 + struct.unpack_from("<i", section.data, offset + 3)[0]
            if target not in table_set:
                continue
            info = diagnostics[target]
            decoded = list(decoder.disasm(section.data[offset:offset + 7], xref, count=1))
            if not decoded or decoded[0].id != X86_INS_LEA or decoded[0].size != 7:
                info["rejected"].append({"xref_rva": xref, "reason": "引用指令无法可靠解码"})
                continue
            insn = decoded[0]
            if len(insn.operands) != 2 or insn.operands[1].type != X86_OP_MEM or insn.operands[1].mem.base != X86_REG_RIP:
                info["rejected"].append({"xref_rva": xref, "reason": "引用不是 RIP 相对 LEA"})
                continue
            if insn.address + insn.size + insn.operands[1].mem.disp != target:
                info["rejected"].append({"xref_rva": xref, "reason": "引用目标校验失败"})
                continue
            candidates = []
            index = bisect_right(starts, xref) - 1
            while index >= 0 and prefix_ends[index] > xref:
                item = bounds[index]
                if item.begin <= xref < item.end:
                    candidates.append(item)
                index -= 1
            if len(candidates) != 1:
                reason = "引用缺少 .pdata 函数边界" if not candidates else "引用存在重叠函数边界"
                info["rejected"].append({"xref_rva": xref, "reason": reason})
                continue
            function = candidates[0]
            reason = _function_reason(function, sections, xref)
            if reason:
                info["rejected"].append({"xref_rva": xref, "reason": reason})
                continue
            # Also reject a byte-pattern hit inside another instruction's operand.
            # Decode from the recorded start to the xref; an invalid/data gap fails
            # closed. Legal leaf NOP padding needs no special prologue assumption.
            begin_offset = function.begin - section.rva
            stream = decoder.disasm(section.data[begin_offset:offset + 7], function.begin)
            found_boundary = False
            for prior in stream:
                if prior.address == xref:
                    found_boundary = prior.id == X86_INS_LEA and prior.size == 7
                    break
            if not found_boundary:
                info["rejected"].append({"xref_rva": xref, "reason": "引用不在可验证指令边界"})
                continue
            entries.add(function.begin)
            info["entry_rvas"].append(function.begin)
    for info in diagnostics.values():
        info["entry_rvas"] = sorted(set(info["entry_rvas"]))
        if not info["entry_rvas"] and not info["rejected"]:
            info["rejected"].append({"reason": "未找到可执行节内的 LEA 引用"})
    return tables, sorted(entries), list(diagnostics.values())


def _file_version(pe) -> str | None:
    try:
        pe.parse_data_directories(directories=[2])
        info = pe.VS_FIXEDFILEINFO[0]
        return ".".join(str(value) for value in (
            info.FileVersionMS >> 16, info.FileVersionMS & 0xFFFF,
            info.FileVersionLS >> 16, info.FileVersionLS & 0xFFFF,
        ))
    except Exception:
        # Version resources are optional and never affect entry validation.
        return None


def _openssl_abi(function: RuntimeFunction, sections: Sequence[ImageSection]) -> tuple[int, dict] | None:
    """Recognize the OpenSSL Win64 SHA-512 block ABI using register data flow.

    Constants and exception metadata alone do not establish an ABI: a different
    SHA-512 wrapper can keep its input in RCX rather than the second argument.
    The supported assembler path preserves ctx/input/blocks in RDI/RSI/RDX,
    forms input + blocks * 128, loads all eight context words, and reads input.
    """
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    from capstone.x86 import (X86_INS_MOV, X86_INS_SHL, X86_INS_LEA,
                             X86_OP_REG, X86_OP_IMM, X86_OP_MEM,
                             X86_REG_RAX, X86_REG_RSP, X86_REG_RCX,
                             X86_REG_RDX, X86_REG_R8, X86_REG_RDI, X86_REG_RSI)

    code = _section_at(sections, function.begin, function.end - function.begin)
    if code is None:
        return None
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    offset = function.begin - code.rva
    instructions = list(decoder.disasm(code.data[offset:offset + min(function.end - function.begin, 4096)], function.begin))

    def reg_move(insn, destination, source):
        return (insn.id == X86_INS_MOV and len(insn.operands) == 2
                and insn.operands[0].type == X86_OP_REG and insn.operands[0].reg == destination
                and insn.operands[1].type == X86_OP_REG and insn.operands[1].reg == source)

    def writes_family(insn, names):
        return bool({insn.reg_name(register) for register in insn.regs_access()[1]} & names)

    rdx_family = {"rdx", "edx", "dx", "dl", "dh"}
    rsi_family = {"rsi", "esi", "si", "sil"}
    rdi_family = {"rdi", "edi", "di", "dil"}

    if len(instructions) < 3 or not all(reg_move(instructions[index], dst, src) for index, (dst, src) in enumerate((
            (X86_REG_RDI, X86_REG_RCX), (X86_REG_RSI, X86_REG_RDX), (X86_REG_RDX, X86_REG_R8)))):
        return None
    scaled_at = end_at = None
    # RDX must retain the copied block count up to multiplication by 16; the
    # address scale by 8 then proves a 128-byte block size, without guessed ABI.
    for index, insn in enumerate(instructions[3:], 3):
        if insn.mnemonic in ("call", "ret", "retf") or writes_family(insn, rsi_family | rdi_family):
            return None
        operands = insn.operands
        if (insn.id == X86_INS_SHL and len(operands) == 2
                and operands[0].type == X86_OP_REG and operands[0].reg == X86_REG_RDX
                and operands[1].type == X86_OP_IMM and operands[1].imm == 4):
            scaled_at = index
            break
        if writes_family(insn, rdx_family):
            return None
    if scaled_at is None:
        return None
    for index, insn in enumerate(instructions[scaled_at + 1:], scaled_at + 1):
        if insn.mnemonic in ("call", "ret", "retf") or writes_family(insn, rsi_family | rdi_family):
            return None
        operands = insn.operands
        if (insn.id == X86_INS_LEA and len(operands) == 2
                and operands[0].type == X86_OP_REG and operands[0].reg == X86_REG_RDX
                and operands[1].type == X86_OP_MEM
                and operands[1].mem.base == X86_REG_RSI
                and operands[1].mem.index == X86_REG_RDX
                and operands[1].mem.scale == 8 and operands[1].mem.disp == 0):
            end_at = index
            break
        if writes_family(insn, rdx_family):
            return None
    if end_at is None:
        return None
    ctx_offsets = set()
    reads_input = False
    required_offsets = set(range(0, 64, 8))
    for insn in instructions[end_at + 1:]:
        if insn.mnemonic in ("call", "ret", "retf"):
            return None
        if insn.id == X86_INS_MOV and len(insn.operands) == 2:
            destination, source = insn.operands
            if destination.type == X86_OP_REG and source.type == X86_OP_MEM and source.size == 8:
                if source.mem.index == 0 and source.mem.base == X86_REG_RDI:
                    ctx_offsets.add(source.mem.disp)
                if source.mem.index == 0 and source.mem.base == X86_REG_RSI and source.mem.disp == 0:
                    reads_input = True
        if not ctx_offsets >= required_offsets and writes_family(insn, rdi_family):
            return None
        if not reads_input and writes_family(insn, rsi_family):
            return None
        if ctx_offsets >= required_offsets and reads_input:
            break
    if not ctx_offsets >= required_offsets or not reads_input:
        return None

    entry = function.begin
    prefix_verified = False
    # OpenSSL's Win64 exception region may begin after its two nonvolatile
    # register saves. Recover only the exact, three-instruction predecessor;
    # never infer a start from arbitrary padding or an unconstrained backscan.
    if code.contains(function.begin - 13, 13):
        predecessor = list(decoder.disasm(code.data[offset - 13:offset], function.begin - 13))

        def stack_save(insn, displacement, source):
            return (insn.id == X86_INS_MOV and len(insn.operands) == 2
                    and insn.operands[0].type == X86_OP_MEM
                    and insn.operands[0].mem.base == X86_REG_RSP
                    and insn.operands[0].mem.index == 0
                    and insn.operands[0].mem.disp == displacement
                    and insn.operands[1].type == X86_OP_REG and insn.operands[1].reg == source)

        if (len(predecessor) == 3 and predecessor[-1].address + predecessor[-1].size == function.begin
                and stack_save(predecessor[0], 8, X86_REG_RDI)
                and stack_save(predecessor[1], 16, X86_REG_RSI)
                and reg_move(predecessor[2], X86_REG_RAX, X86_REG_RSP)):
            entry -= 13
            prefix_verified = True
    if not prefix_verified:
        # RDI/RSI are nonvolatile in Win64. The recognized argument moves
        # overwrite them, so accepting that point without their proven save
        # predecessor would claim a true callable entry without evidence.
        return None
    return entry, {
        "abi": "openssl_sha512_win64", "pdata_entry_rva": function.begin,
        "win64_save_prefix_verified": prefix_verified,
        "evidence": ["RCX ctx → RDI", "RDX input → RSI", "R8 blocks → RDX",
                     "input + blocks × 128", "8 context words at RDI+0..56", "64-bit input read at RSI"],
    }


def validate_entry_abis(sections: Sequence[ImageSection], functions: Sequence[RuntimeFunction],
                        candidates: Sequence[int], *, allow_test_fixture: bool = False) -> tuple[list[int], dict, list[dict]]:
    """Filter static candidates; the synthetic opt-in is never a product option."""
    entries = set()
    metadata = {}
    excluded = []
    for candidate in candidates:
        matching = [function for function in functions if function.begin == candidate]
        verified = _openssl_abi(matching[0], sections) if len(matching) == 1 else None
        if verified is None and allow_test_fixture and len(matching) == 1:
            function = matching[0]
            code = _section_at(sections, candidate, function.end - candidate)
            body = code.data[candidate - code.rva:function.end - code.rva] if code else b""
            # Explicitly recognize only our inert NOP/LEA/return fixture, rather
            # than letting this option bless arbitrary unknown production ABIs.
            if (len(body) == 45 and body[:32] == b"\x90" * 32 and body[32:35] == b"\x48\x8d\x05"
                    and body[39:] == b"\xb8\x01\x00\x00\x00\xc3"):
                verified = candidate, {"abi": "synthetic_test_only", "pdata_entry_rva": candidate,
                                       "evidence": ["inert NOP + LEA + return fixture; explicit test opt-in"]}
        if verified is None:
            excluded.append({"candidate_rva": candidate, "reason": "未证实 RCX/RDX/R8 的 OpenSSL SHA-512 调用约定"})
            continue
        entry, info = verified
        if info["abi"] == "openssl_sha512_win64" and any(
                function.begin < candidate and function.end > entry for function in functions):
            excluded.append({"candidate_rva": candidate, "reason": "入口保存前缀与其他 .pdata 函数边界重叠"})
            continue
        if _section_at(sections, entry, 32) is None:
            excluded.append({"candidate_rva": candidate, "reason": "入口代码签名长度不足"})
            continue
        entries.add(entry)
        metadata[str(entry)] = info
    return sorted(entries), metadata, excluded


def resolve_image(path: str | Path, *, allow_test_fixture: bool = False) -> dict:
    """Read and hash an image, returning only verified static locations."""
    import pefile

    try:
        source = Path(path).expanduser().resolve(strict=True)
        blob = source.read_bytes()
        pe = pefile.PE(data=blob, fast_load=True)
    except (OSError, pefile.PEFormatError) as exc:
        raise ValueError("无法读取有效的微信模块文件，请重新选择 Weixin.dll。") from exc
    try:
        machine = int(pe.FILE_HEADER.Machine)
        if machine != IMAGE_FILE_MACHINE_AMD64 or pe.OPTIONAL_HEADER.Magic != 0x20B:
            raise ValueError("登录采集目前仅支持 Windows x64 微信模块。")
        image_size = int(pe.OPTIONAL_HEADER.SizeOfImage)
        if not 0 < image_size <= 0xFFFFFFFF:
            raise ValueError("模块映像大小异常，已停止定位。")
        sections: list[ImageSection] = []
        for section in pe.sections:
            rva = int(section.VirtualAddress)
            raw_start, raw_size = int(section.PointerToRawData), int(section.SizeOfRawData)
            size = min(raw_size, int(section.Misc_VirtualSize) or raw_size)
            if raw_start < 0 or raw_start + raw_size > len(blob) or rva + size > image_size:
                raise ValueError("模块节边界不完整，已停止定位。")
            sections.append(ImageSection(
                section.Name.rstrip(b"\0").decode("ascii", errors="replace"), rva,
                blob[raw_start:raw_start + size], int(section.Characteristics),
            ))
        if len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) <= 3:
            raise ValueError("模块缺少可信的 .pdata 函数边界，已停止定位。")
        directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
        directory_rva, directory_size = int(directory.VirtualAddress), int(directory.Size)
        pdata = _section_at(sections, directory_rva, directory_size)
        if (not directory_size or directory_size % 12 or directory_size > 16 * 1024 * 1024
                or pdata is None or pdata.name != ".pdata" or not pdata.characteristics & IMAGE_SCN_MEM_READ):
            raise ValueError("模块缺少可信的 .pdata 函数边界，已停止定位。")
        start = directory_rva - pdata.rva
        functions = [
            RuntimeFunction(*values) for values in
            struct.iter_unpack("<III", pdata.data[start:start + directory_size])
            if values[0] < values[1] <= image_size and values[2] < image_size
        ]
        tables, candidates, diagnostics = locate_entries(sections, functions)
        entries, abi_metadata, excluded = validate_entry_abis(sections, functions, candidates, allow_test_fixture=allow_test_fixture)
        if not entries:
            raise ValueError("未找到经 SHA-512 常量表、指令、.pdata 边界与调用约定共同验证的入口；当前微信版本暂不支持登录采集。")
        signatures = {}
        for entry in entries:
            code = _section_at(sections, entry, 32)
            signatures[str(entry)] = code.data[entry - code.rva:entry - code.rva + 32].hex()
        for item in diagnostics:
            item["candidate_entry_rvas"] = item["entry_rvas"]
            item["entry_rvas"] = [entry for entry in entries if abi_metadata[str(entry)]["pdata_entry_rva"] in item["candidate_entry_rvas"]]
            item["rejected"].extend({"xref_entry_rva": entry["candidate_rva"], "reason": entry["reason"]}
                                    for entry in excluded if entry["candidate_rva"] in item["candidate_entry_rvas"])
        return {
            "module_name": source.name, "module_path": str(source),
            "sha256": hashlib.sha256(blob).hexdigest(), "image_size": image_size,
            "machine": machine, "file_version": _file_version(pe),
            "table_rvas": tables, "entry_rvas": entries, "table_entries": diagnostics,
            "entry_signatures": signatures, "entry_abis": abi_metadata, "excluded": excluded,
            "test_fixture": any(info["abi"] == "synthetic_test_only" for info in abi_metadata.values()),
        }
    finally:
        pe.close()
