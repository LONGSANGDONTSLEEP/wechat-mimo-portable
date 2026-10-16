from pathlib import Path
import struct
import tempfile
import unittest

import capture_locator as locator


READ = locator.IMAGE_SCN_MEM_READ
EXEC = locator.IMAGE_SCN_MEM_EXECUTE
WRITE = locator.IMAGE_SCN_MEM_WRITE


def section(name, rva, data, flags=READ):
    return locator.ImageSection(name, rva, bytes(data), flags)


def lea(address, target, rex=0x48, modrm=0x05):
    return bytes((rex, 0x8D, modrm)) + struct.pack("<i", target - address - 7)


def example_sections(*, rex=0x48, modrm=0x05, xref=0x1040, target=0x2000):
    code = bytearray(b"\x90" * 256)
    code[xref - 0x1000:xref - 0x1000 + 7] = lea(xref, target, rex, modrm)
    return [section(".text", 0x1000, code, READ | EXEC),
            section(".rdata", 0x2000, locator.SHA512_TABLE_PREFIX + b"\0" * 64),
            section(".xdata", 0x3000, b"\x01\x00\x00\x00")]


def abi_sections(*, with_save_prefix=True, inserted=b"", missing_ctx=False):
    """Inert bytes expressing the supported argument flow, never executed."""
    prefix = bytes.fromhex("48897c24084889742410488bc4") if with_save_prefix else b""
    body = bytes.fromhex("4889cf4889d64c89c2") + inserted
    body += bytes.fromhex("48c1e204488d14d6")
    for offset in range(0, 64 - (8 if missing_ctx else 0), 8):
        body += bytes.fromhex("488b07") if offset == 0 else bytes.fromhex("488b47") + bytes((offset,))
    body += bytes.fromhex("488b06")
    xref = 0x1000 + len(prefix) + len(body)
    body += lea(xref, 0x2000) + b"\xc3"
    parts = [section(".text", 0x1000, prefix + body, READ | EXEC),
             section(".rdata", 0x2000, locator.SHA512_TABLE_PREFIX),
             section(".xdata", 0x3000, b"\x01\x00\x00\x00")]
    function = locator.RuntimeFunction(0x1000 + len(prefix), 0x1000 + len(prefix) + len(body), 0x3000)
    return parts, function


class LocatorTests(unittest.TestCase):
    def test_two_tables_and_all_rex_registers(self):
        sections = example_sections()
        code = bytearray(sections[0].data)
        code[0x80:0x87] = lea(0x1080, 0x2080, 0x4C, 0x3D)
        sections[0] = section(".text", 0x1000, code, READ | EXEC)
        sections[1] = section(".rdata", 0x2000, locator.SHA512_TABLE_PREFIX + b"\0" * 64 + locator.SHA512_TABLE_PREFIX)
        functions = [locator.RuntimeFunction(0x1020, 0x1060, 0x3000), locator.RuntimeFunction(0x1070, 0x10A0, 0x3000)]
        tables, entries, diagnostics = locator.locate_entries(sections, functions)
        self.assertEqual(tables, [0x2000, 0x2080])
        self.assertEqual(entries, [0x1020, 0x1070])
        self.assertEqual(diagnostics[1]["entry_rvas"], [0x1070])
        for rex in (0x48, 0x4C):
            for modrm in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D):
                with self.subTest(rex=rex, modrm=modrm):
                    parts = example_sections(rex=rex, modrm=modrm)
                    self.assertEqual(locator.locate_entries(parts, functions[:1])[1], [0x1020])

    def test_executable_table_is_accepted_but_writable_decoys_are_not(self):
        self.assertEqual(locator.find_tables([
            section(".text", 0x1000, locator.SHA512_TABLE_PREFIX, READ | EXEC),
            section(".data", 0x2000, locator.SHA512_TABLE_PREFIX, READ | WRITE),
            section(".none", 0x3000, locator.SHA512_TABLE_PREFIX, 0),
        ]), [0x1000])

    def test_first_constant_alone_does_not_match(self):
        data = locator.SHA512_TABLE_PREFIX[:8] + b"\0" * 24
        self.assertEqual(locator.find_tables([section(".rdata", 0x2000, data)]), [])

    def test_simd_pair_repetition_returns_true_table_start(self):
        for lanes in (2, 4):
            data = b"".join(locator.SHA512_TABLE_PREFIX[offset:offset + 16] * lanes for offset in range(0, 64, 16))
            with self.subTest(lanes=lanes):
                self.assertEqual(locator.find_tables([section(".text", 0x2000, data, READ | EXEC)]), [0x2000])

    def test_no_pdata_and_overlapping_pdata_are_rejected(self):
        parts = example_sections()
        self.assertEqual(locator.locate_entries(parts, [])[1], [])
        overlapping = [locator.RuntimeFunction(0x1000, 0x1080, 0x3000), locator.RuntimeFunction(0x1020, 0x1060, 0x3000)]
        _, entries, diagnostics = locator.locate_entries(parts, overlapping)
        self.assertEqual(entries, [])
        self.assertIn("重叠", diagnostics[0]["rejected"][0]["reason"])

    def test_unwind_and_function_bounds_fail_closed(self):
        parts = example_sections()
        for boundary in (locator.RuntimeFunction(0x1020, 0x1045, 0x3000),
                         locator.RuntimeFunction(0x1020, 0x1060, 0x4000),
                         locator.RuntimeFunction(0x1020, 0x100000, 0x3000)):
            with self.subTest(boundary=boundary):
                self.assertEqual(locator.locate_entries(parts, [boundary])[1], [])
        parts[-1] = section(".xdata", 0x3000, b"\x00" * 4)
        self.assertEqual(locator.locate_entries(parts, [locator.RuntimeFunction(0x1020, 0x1060, 0x3000)])[1], [])

    def test_match_inside_immediate_is_not_an_instruction_boundary(self):
        parts = example_sections()
        code = bytearray(parts[0].data)
        # movabs rax, imm64: the embedded seven bytes resemble a valid LEA.
        code[0x3E:0x40] = b"\x48\xb8"
        parts[0] = section(".text", 0x1000, code, READ | EXEC)
        self.assertEqual(locator.locate_entries(parts, [locator.RuntimeFunction(0x1020, 0x1060, 0x3000)])[1], [])

    def test_non_executable_reference_and_wrong_target_are_ignored(self):
        parts = example_sections(target=0x2010)
        self.assertEqual(locator.locate_entries(parts, [locator.RuntimeFunction(0x1020, 0x1060, 0x3000)])[1], [])
        parts = example_sections()
        parts[0] = section(".text", 0x1000, parts[0].data, READ)
        self.assertEqual(locator.locate_entries(parts, [locator.RuntimeFunction(0x1020, 0x1060, 0x3000)])[1], [])

    def test_malformed_file_has_localized_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Weixin.dll"
            path.write_bytes(b"not a PE")
            with self.assertRaisesRegex(ValueError, "有效"):
                locator.resolve_image(path)
            with self.assertRaisesRegex(ValueError, "有效"):
                locator.resolve_image(Path(folder) / "missing.dll")

    def test_verified_openssl_abi_recovers_only_exact_save_prefix(self):
        parts, function = abi_sections()
        entries, metadata, excluded = locator.validate_entry_abis(parts, [function], [function.begin])
        self.assertEqual(entries, [0x1000])
        self.assertEqual(excluded, [])
        self.assertEqual(metadata[str(0x1000)]["abi"], "openssl_sha512_win64")
        self.assertTrue(metadata[str(0x1000)]["win64_save_prefix_verified"])
        parts, function = abi_sections(with_save_prefix=False)
        self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin])[0], [])
        parts, function = abi_sections()
        corrupt = bytearray(parts[0].data)
        corrupt[4] = 16  # Different stack slot: do not recover the preceding 13B.
        parts[0] = section(".text", 0x1000, corrupt, READ | EXEC)
        self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin])[0], [])

    def test_save_prefix_must_not_overlap_another_runtime_function(self):
        parts, function = abi_sections()
        previous = locator.RuntimeFunction(0x0FF0, function.begin, 0x3000)
        entries, _, excluded = locator.validate_entry_abis(parts, [previous, function], [function.begin])
        self.assertEqual(entries, [])
        self.assertIn("重叠", excluded[0]["reason"])

    def test_unknown_sha512_wrapper_abi_is_excluded(self):
        parts = example_sections()
        function = locator.RuntimeFunction(0x1020, 0x1060, 0x3000)
        entries, metadata, excluded = locator.validate_entry_abis(parts, [function], [function.begin])
        self.assertEqual(entries, [])
        self.assertEqual(metadata, {})
        self.assertIn("调用约定", excluded[0]["reason"])

    def test_partial_argument_register_clobber_and_missing_context_fail(self):
        for inserted in (b"\x31\xd2", b"\x31\xf6", b"\x31\xff", b"\xe8\x00\x00\x00\x00"):
            with self.subTest(inserted=inserted):
                parts, function = abi_sections(inserted=inserted)
                self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin])[0], [])
        parts, function = abi_sections(missing_ctx=True)
        self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin])[0], [])

    def test_synthetic_fixture_requires_explicit_flag_and_exact_body(self):
        body = b"\x90" * 32 + lea(0x1020, 0x2000) + b"\xb8\x01\x00\x00\x00\xc3"
        parts = [section(".text", 0x1000, body, READ | EXEC)]
        function = locator.RuntimeFunction(0x1000, 0x1000 + len(body), 0x3000)
        self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin])[0], [])
        entries, metadata, excluded = locator.validate_entry_abis(parts, [function], [function.begin], allow_test_fixture=True)
        self.assertEqual(entries, [0x1000])
        self.assertEqual(metadata[str(0x1000)]["abi"], "synthetic_test_only")
        self.assertEqual(excluded, [])
        parts[0] = section(".text", 0x1000, b"\xcc" + body[1:], READ | EXEC)
        self.assertEqual(locator.validate_entry_abis(parts, [function], [function.begin], allow_test_fixture=True)[0], [])


if __name__ == "__main__":
    unittest.main()
