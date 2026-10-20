#!/usr/bin/env python3
"""Tests for the dependency-free QR generator in qr_code.py.

Run:  python3 test_qr_code.py
Prints PASS/FAIL per test and exits nonzero if any required test fails.
Prove-fail:  python3 test_qr_code.py --prove-fail
That corrupts format information in memory and requires the structural test
to go red.
Optional checks are SKIPped when their tool is unavailable:
  * PNG checks need Pillow.
  * Matrix cross-check needs the separately installed ``qrcode`` package;
    production code does NOT import it.
  * Round-trip decode needs Pillow and either pyzbar or libzbar.so.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qr_code  # noqa: E402  (path must be set first)

SVG = "{http://www.w3.org/2000/svg}"
URL = "https://example.com/boardwalk/share?src=qr_selftest"
SHORT_URL = "https://e.co"
LONG_URL = "https://example.com/" + "a" * 280  # exactly 300 ASCII bytes
TINY_URL = "https://e.co"

try:
    from PIL import Image
except ImportError:  # pragma: no cover - only on hosts without Pillow
    Image = None

try:
    import qrcode as _qrcode
    from qrcode.util import MODE_8BIT_BYTE as _MODE_BYTE
    from qrcode.util import QRData as _QRData
except Exception:  # pragma: no cover - optional oracle
    _qrcode = None
    _QRData = None
    _MODE_BYTE = None


class SkipTest(Exception):
    """Raised when an optional test dependency is not installed."""


_TESTS = []


def test(function):
    _TESTS.append(function)
    return function


def _bch_remainder(value: int, generator: int, degree: int) -> int:
    """Independent polynomial-division BCH remainder for structural checks."""
    value <<= degree
    for bit in range(value.bit_length() - 1, degree - 1, -1):
        if (value >> bit) & 1:
            value ^= generator << (bit - degree)
    return value


def _format_coords(size: int):
    first = []
    second = []
    for i in range(15):
        if i < 6:
            first.append((i, 8))
        elif i < 8:
            first.append((i + 1, 8))
        else:
            first.append((size - 15 + i, 8))
        if i < 8:
            second.append((8, size - 1 - i))
        elif i < 9:
            second.append((8, 7))
        else:
            second.append((8, 14 - i))
    return tuple(first), tuple(second)


def _bits_at(matrix, coords) -> int:
    return sum((1 if matrix[row][col] else 0) << i for i, (row, col) in enumerate(coords))


def _finder_dark(dx: int, dy: int) -> bool:
    return (
        0 <= dx <= 6
        and 0 <= dy <= 6
        and (dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4))
    )


def _load_decoder():
    """Return (label, callable(png_bytes)->str|None) or (None, None)."""
    if Image is None:
        return None, None

    try:  # Prefer the conventional Python binding when present.
        from pyzbar.pyzbar import decode as pyzbar_decode

        def decode_with_pyzbar(png: bytes):
            image = Image.open(io.BytesIO(png))
            image.load()
            symbols = pyzbar_decode(image)
            if not symbols:
                return None
            return symbols[0].data.decode("utf-8")

        return "pyzbar", decode_with_pyzbar
    except Exception:
        pass

    path = ctypes.util.find_library("zbar")
    if path is None:
        for candidate in ("libzbar.so.0", "libzbar.so"):
            try:
                ctypes.CDLL(candidate)
            except OSError:
                continue
            path = candidate
            break
    if path is None:
        return None, None

    try:
        lib = ctypes.CDLL(path)
        lib.zbar_image_scanner_create.restype = ctypes.c_void_p
        lib.zbar_image_scanner_destroy.argtypes = [ctypes.c_void_p]
        lib.zbar_image_create.restype = ctypes.c_void_p
        lib.zbar_image_destroy.argtypes = [ctypes.c_void_p]
        lib.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
        lib.zbar_image_first_symbol.restype = ctypes.c_void_p
        lib.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_data.restype = ctypes.c_char_p
        lib.zbar_symbol_get_data_length.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_data_length.restype = ctypes.c_uint
        lib.zbar_image_scanner_set_config.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.zbar_image_set_size.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        lib.zbar_image_set_data.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        lib.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.zbar_scan_image.restype = ctypes.c_int
    except (AttributeError, OSError):
        return None, None

    def decode_with_libzbar(png: bytes):
        image = Image.open(io.BytesIO(png)).convert("L")
        width, height = image.size
        raw = image.tobytes()
        scanner = lib.zbar_image_scanner_create()
        zbar_image = lib.zbar_image_create()
        if not scanner or not zbar_image:
            raise RuntimeError("libzbar could not create scanner/image")
        try:
            lib.zbar_image_scanner_set_config(scanner, 0, 0, 1)  # enable all
            lib.zbar_image_set_format(zbar_image, 0x30303859)  # 'Y800'
            lib.zbar_image_set_size(zbar_image, width, height)
            # Keep `raw` alive until scanning is complete; no cleanup callback.
            pixel_buffer = ctypes.create_string_buffer(raw)
            lib.zbar_image_set_data(
                zbar_image,
                ctypes.cast(pixel_buffer, ctypes.c_void_p),
                len(raw),
                None,
            )
            if lib.zbar_scan_image(scanner, zbar_image) <= 0:
                return None
            symbol = lib.zbar_image_first_symbol(zbar_image)
            if not symbol:
                return None
            data = lib.zbar_symbol_get_data(symbol)
            length = lib.zbar_symbol_get_data_length(symbol)
            return ctypes.string_at(data, length).decode("utf-8")
        finally:
            lib.zbar_image_destroy(zbar_image)
            lib.zbar_image_scanner_destroy(scanner)

    return f"libzbar via ctypes ({path})", decode_with_libzbar


_DECODER = None
_DECODER_LABEL = "uninitialized"


def _decoder():
    global _DECODER, _DECODER_LABEL
    if _DECODER is None:
        _DECODER_LABEL, decoder = _load_decoder()
        _DECODER = decoder
    return _DECODER


def _reference(payload: str, version: int, mask: int):
    if _qrcode is None:
        raise SkipTest("qrcode package is not installed")
    qr = _qrcode.QRCode(
        version=version,
        error_correction=_qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=0,
        mask_pattern=mask,
    )
    qr.add_data(_QRData(payload, mode=_MODE_BYTE))
    qr.make(fit=False)
    return qr.get_matrix()


@test
def test_svg_structure_and_quiet_zone():
    symbol = qr_code._encode(URL)
    svg = qr_code.qr_svg(URL, scale=5, border=4)
    assert qr_code.qr_svg(URL, scale=5, border=4) == svg, "SVG is not deterministic"

    root = ET.fromstring(svg)
    assert root.tag == SVG + "svg", f"unexpected root: {root.tag}"
    span = symbol.size + 8
    assert root.get("viewBox") == f"0 0 {span} {span}", "wrong viewBox"
    assert root.get("width") == str(span * 5), "wrong width"
    assert root.get("height") == str(span * 5), "wrong height"
    assert root.get("data-qr-version") == str(symbol.version)
    assert root.get("data-qr-mask") == str(symbol.mask)
    assert root.get("data-qr-ecc") == "M"

    paths = root.findall(f".//{SVG}path")
    assert len(paths) == 1, "expected exactly one dark-module path"
    assert paths[0].get("fill") == "#000"
    data = paths[0].get("d", "")
    assert data.startswith("M"), "empty SVG path"

    for tag in ("script", "image", "use", "foreignObject"):
        assert not root.findall(f".//{SVG}{tag}"), f"unexpected <{tag}> element"
    for element in root.iter():
        for attribute in element.attrib:
            assert not attribute.endswith("href"), "SVG must not reference external data"

    # Reconstruct the matrix from the SVG path and compare it exactly.
    commands = re.findall(r"M(\d+) (\d+)h(\d+)v1h-\3z", data)
    assert commands, "path has no module-run commands"
    rendered = [[False] * symbol.size for _ in range(symbol.size)]
    for x_text, y_text, width_text in commands:
        x, y, width = int(x_text), int(y_text), int(width_text)
        assert 4 <= x and x + width <= 4 + symbol.size, "run crosses right quiet zone"
        assert 4 <= y < 4 + symbol.size, "run is outside the symbol or quiet zone"
        for col in range(x - 4, x - 4 + width):
            assert not rendered[y - 4][col], "overlapping path runs"
            rendered[y - 4][col] = True
    assert rendered == symbol.modules, "SVG path does not represent the QR matrix"
    assert sum(int(width) for _x, _y, width in commands) == sum(
        sum(1 for cell in row if cell) for row in symbol.modules
    )

    zero_border = ET.fromstring(qr_code.qr_svg(TINY_URL, scale=1, border=0))
    assert zero_border.get("viewBox") == f"0 0 {qr_code._encode(TINY_URL).size} " \
        f"{qr_code._encode(TINY_URL).size}", "border=0 geometry is wrong"


@test
def test_known_answer_structure():
    symbol = qr_code._encode(SHORT_URL, version=2)
    matrix = symbol.modules
    size = symbol.size
    assert size == 25, "version 2 must be 25x25"

    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dy in range(7):
            for dx in range(7):
                assert matrix[top + dy][left + dx] == _finder_dark(dx, dy), (
                    f"finder mismatch at {top + dy},{left + dx}"
                )

    # Separators around the three finders must be light.
    assert all(not matrix[7][col] for col in range(8))
    assert all(not matrix[row][7] for row in range(8))
    assert all(not matrix[7][col] for col in range(size - 8, size))
    assert all(not matrix[row][size - 8] for row in range(8))
    assert all(not matrix[size - 8][col] for col in range(8))
    assert all(not matrix[row][7] for row in range(size - 8, size))

    # Version 2 has a single alignment pattern, at (18,18), away from timing.
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            expected = max(abs(dx), abs(dy)) != 1
            assert matrix[18 + dy][18 + dx] == expected, "alignment pattern mismatch"

    # Timing patterns in row 6 and column 6.
    for i in range(8, size - 8):
        assert matrix[6][i] == (i % 2 == 0), "horizontal timing mismatch"
        assert matrix[i][6] == (i % 2 == 0), "vertical timing mismatch"

    # Fixed dark module and both format-information copies.
    assert matrix[size - 8][8] is True, "fixed dark module is not dark"
    first, second = _format_coords(size)
    format_bits = _bits_at(matrix, first)
    assert format_bits == _bits_at(matrix, second), "format copies disagree"
    raw = format_bits ^ 0x5412
    data = raw >> 10
    assert data >> 3 == 0, "format bits do not declare level M"
    assert data & 7 == symbol.mask, "format bits declare the wrong mask"
    expected_raw = (data << 10) | _bch_remainder(data, 0x537, 10)
    assert format_bits == (expected_raw ^ 0x5412), "format BCH is invalid"

    # Version-information BCH for version 7, in both copies.
    version_symbol = qr_code._encode(SHORT_URL, version=7)
    version_bits = (7 << 12) | _bch_remainder(7, 0x1F25, 12)
    first_copy = 0
    second_copy = 0
    for i in range(18):
        row, col = i // 3, version_symbol.size - 11 + i % 3
        first_copy |= (1 if version_symbol.modules[row][col] else 0) << i
        row, col = version_symbol.size - 11 + i % 3, i // 3
        second_copy |= (1 if version_symbol.modules[row][col] else 0) << i
    assert first_copy == version_bits, "version information copy 1 is invalid"
    assert second_copy == version_bits, "version information copy 2 is invalid"


@test
def test_payload_length_and_limits():
    assert len(LONG_URL) == 300 and len(LONG_URL.encode("utf-8")) == 300
    long_symbol = qr_code._encode(LONG_URL)
    assert long_symbol.version == 13, "300 bytes should use version 13-M"
    assert long_symbol.size == 69
    assert qr_code.qr_svg(LONG_URL).startswith("<?xml"), "300-byte URL did not render"

    assert qr_code.MAX_PAYLOAD_BYTES == 412, "unexpected maximum payload"
    maximum = "m" * qr_code.MAX_PAYLOAD_BYTES
    max_symbol = qr_code._encode(maximum)
    assert max_symbol.version == 15
    assert max_symbol.size == 77
    assert qr_code.qr_svg(maximum).startswith("<?xml")

    for bad, error in [
        ("", ValueError),
        (None, TypeError),
        (b"https://e.co", TypeError),
        ("x" * (qr_code.MAX_PAYLOAD_BYTES + 1), ValueError),
    ]:
        try:
            qr_code.qr_svg(bad)
        except error:
            pass
        else:
            raise AssertionError(f"expected {error.__name__} for {type(bad).__name__}")

    for scale, border in (
        (0, 4),
        (-1, 4),
        (float("nan"), 4),
        (float("inf"), 4),
        (6, -1),
        (6, 1.5),
    ):
        try:
            qr_code.qr_svg(URL, scale=scale, border=border)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for scale={scale}, border={border}")


@test
def test_png_rendering():
    if Image is None:
        raise SkipTest("Pillow is not installed")
    symbol = qr_code._encode(URL)
    scale, border = 4, 3
    png = qr_code.qr_png(URL, scale=scale, border=border)
    assert isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n")

    image = Image.open(io.BytesIO(png))
    image.load()
    span = symbol.size + 2 * border
    assert image.size == (span * scale, span * scale), "wrong PNG dimensions"
    assert image.mode == "L", "PNG should be 8-bit grayscale"
    assert image.getpixel((0, 0)) == 255, "quiet zone must be white"
    assert image.getpixel((border * scale, border * scale)) == 0, (
        "top-left finder must be black"
    )

    # Sample the centre of every module and compare with the symbol matrix.
    for row in range(symbol.size):
        for col in range(symbol.size):
            x = (col + border) * scale + scale // 2
            y = (row + border) * scale + scale // 2
            expected = 0 if symbol.modules[row][col] else 255
            assert image.getpixel((x, y)) == expected, (
                f"PNG pixel mismatch at module {row},{col}"
            )

    try:
        qr_code.qr_png(URL, scale=1.5)
    except ValueError:
        pass
    else:
        raise AssertionError("fractional PNG scale must be rejected")


@test
def test_matches_qrcode_oracle():
    if _qrcode is None:
        raise SkipTest("qrcode package is not installed")

    # Every supported version and every mask, compared module-for-module.
    for version in range(1, 16):
        for mask in range(8):
            ours = qr_code._encode(SHORT_URL, version=version, mask=mask).modules
            reference = _reference(SHORT_URL, version, mask)
            assert ours == reference, f"matrix differs at version {version}, mask {mask}"

    # Automatic version and mask selection over the full payload range.
    for length in (1, 14, 51, 100, 300, 412):
        payload = "q" * length
        ours = qr_code._encode(payload)
        reference = _reference(payload, ours.version, ours.mask)
        assert ours.modules == reference, (
            f"matrix differs for automatic payload length {length}"
        )


@test
def test_round_trip_decode():
    if Image is None:
        raise SkipTest("Pillow is not installed, so no PNG can be fed to a decoder")
    decoder = _decoder()
    if decoder is None:
        raise SkipTest("no QR decoder found (pyzbar or libzbar)")

    for payload in (URL, LONG_URL):
        png = qr_code.qr_png(payload, scale=6, border=4)
        decoded = decoder(png)
        assert decoded is not None, f"decoder found no QR in {len(payload)}-byte case"
        assert decoded == payload, (
            f"round-trip mismatch for {len(payload)}-byte payload: "
            f"{decoded!r} != {payload!r}"
        )


def prove_fail() -> int:
    """Break a guarded invariant in memory and require the suite to detect it."""
    original = qr_code._format_bits
    try:
        qr_code._format_bits = lambda _mask: 0
        try:
            test_known_answer_structure()
        except AssertionError as exc:
            print(f"PROVE-FAIL PASS (suite went red): {type(exc).__name__}: {exc}")
            return 0
        print("PROVE-FAIL FAIL: corrupted format information was not detected")
        return 1
    finally:
        qr_code._format_bits = original


def main() -> int:
    decoder = _decoder()
    print(f"Pillow: {'available' if Image is not None else 'missing'}")
    print(f"Decoder: {_DECODER_LABEL if decoder is not None else 'none'}")
    print(f"Encoder oracle: {'qrcode installed' if _qrcode is not None else 'none'}")
    print()

    failures = []
    skipped = 0
    for function in _TESTS:
        try:
            function()
        except SkipTest as exc:
            skipped += 1
            print(f"SKIP {function.__name__}: {exc}")
        except Exception as exc:
            failures.append(function.__name__)
            print(f"FAIL {function.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"PASS {function.__name__}")

    passed = len(_TESTS) - len(failures) - skipped
    print()
    print(f"Summary: {passed} passed, {len(failures)} failed, {skipped} skipped "
          f"({len(_TESTS)} total)")
    if failures:
        print(f"FAIL ({', '.join(failures)})")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    if "--prove-fail" in sys.argv:
        raise SystemExit(prove_fail())
    raise SystemExit(main())
