"""Offline, dependency-free QR Code 2 generator with SVG and optional PNG output.

Pure-Python encoder: byte mode, error-correction level M, QR versions 1-15
(automatic version selection).  No third-party package is imported at module
load time.  ``qr_png`` imports Pillow (PIL) only when called and raises a clear
error when Pillow is not installed; SVG output needs no dependencies.

The payload is encoded as UTF-8 bytes in QR byte mode, without an ECI segment.
ASCII URLs are the intended use; percent-encode non-ASCII URL characters so
scanners do not have to guess the byte encoding.  Maximum payload is 412
UTF-8 bytes at version 15-M (a 300-byte URL fits at version 13-M).  The SVG
uses a four-module quiet zone and no external references, scripts, or embedded
resources.

Public API:
    qr_svg(text, scale=6, border=4) -> str
    qr_png(text, scale=6, border=4) -> bytes   (requires Pillow)
    MAX_PAYLOAD_BYTES

Security note: this module renders the supplied URL; it does not log or print
it.  Callers are responsible for access control on the serving endpoint.
"""

from __future__ import annotations

import io
import math
from functools import lru_cache

__all__ = ["MAX_PAYLOAD_BYTES", "qr_png", "qr_svg"]

_ECC_FORMAT_M = 0b00
_FORMAT_MASK = 0x5412
_FORMAT_GEN = 0x537
_VERSION_GEN = 0x1F25

# Reed-Solomon block layout for error-correction level M, versions 1-15.
# Each entry is (block count, total codewords, data codewords).
_RS_BLOCKS_M = {
    1: ((1, 26, 16),),
    2: ((1, 44, 28),),
    3: ((1, 70, 44),),
    4: ((2, 50, 32),),
    5: ((2, 67, 43),),
    6: ((4, 43, 27),),
    7: ((4, 49, 31),),
    8: ((2, 60, 38), (2, 61, 39)),
    9: ((3, 58, 36), (2, 59, 37)),
    10: ((4, 69, 43), (1, 70, 44)),
    11: ((1, 80, 50), (4, 81, 51)),
    12: ((6, 58, 36), (2, 59, 37)),
    13: ((8, 59, 37), (1, 60, 38)),
    14: ((4, 64, 40), (5, 65, 41)),
    15: ((5, 65, 41), (5, 66, 42)),
}

# Alignment-pattern centre coordinates (ISO/IEC 18004 table).
_ALIGNMENT = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
    11: (6, 30, 54),
    12: (6, 32, 58),
    13: (6, 34, 62),
    14: (6, 26, 46, 66),
    15: (6, 26, 48, 70),
}

# Unused remainder bits in the last codeword (ISO/IEC 18004).
_REMAINDER_BITS = {
    1: 0,
    2: 7,
    3: 7,
    4: 7,
    5: 7,
    6: 7,
    7: 0,
    8: 0,
    9: 0,
    10: 0,
    11: 0,
    12: 0,
    13: 0,
    14: 3,
    15: 3,
}

_MASKS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row * col) % 3 + (row + col) % 2) % 2 == 0,
)


def _capacity_bits(version: int) -> int:
    """Number of data bits available at level M for *version*."""
    return 8 * sum(count * data for count, _total, data in _RS_BLOCKS_M[version])


def _max_payload_bytes() -> int:
    best = 0
    for version in _RS_BLOCKS_M:
        header = 4 + (8 if version < 10 else 16)
        best = max(best, (_capacity_bits(version) - header) // 8)
    return best


MAX_PAYLOAD_BYTES = _max_payload_bytes()


# Galois-field arithmetic for GF(256) with primitive polynomial 0x11D.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, len(_EXP)):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if not a or not b:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


@lru_cache(maxsize=None)
def _rs_generator(ec_count: int) -> tuple[int, ...]:
    """Return Reed-Solomon generator coefficients, high degree first."""
    poly = [1]
    for i in range(ec_count):
        factor = _EXP[i]
        result = [0] * (len(poly) + 1)
        for j, coefficient in enumerate(poly):
            result[j] ^= coefficient
            result[j + 1] ^= _gf_mul(coefficient, factor)
        poly = result
    return tuple(poly)


def _rs_ecc(data: list[int], ec_count: int) -> list[int]:
    """Compute *ec_count* Reed-Solomon error-correction codewords."""
    poly = _rs_generator(ec_count)
    remainder = list(data) + [0] * ec_count
    for i in range(len(data)):
        coefficient = remainder[i]
        if coefficient:
            for j, gen in enumerate(poly):
                remainder[i + j] ^= _gf_mul(gen, coefficient)
    return remainder[-ec_count:]


def _pick_version(payload_size: int) -> int:
    for version in sorted(_RS_BLOCKS_M):
        header = 4 + (8 if version < 10 else 16)
        if header + 8 * payload_size <= _capacity_bits(version):
            return version
    raise ValueError(
        f"payload is {payload_size} UTF-8 bytes; maximum supported is "
        f"{MAX_PAYLOAD_BYTES} bytes at version 15-M"
    )


def _format_bits(mask: int) -> int:
    data = (_ECC_FORMAT_M << 3) | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * _FORMAT_GEN)
    return ((data << 10) | remainder) ^ _FORMAT_MASK


def _version_bits(version: int) -> int:
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * _VERSION_GEN)
    return (version << 12) | remainder


class _Symbol:
    """A built QR symbol: square matrix with a four-module quiet zone omitted."""

    def __init__(self, payload: bytes, version: int, forced_mask: int | None):
        self.version = version
        self.size = 17 + 4 * version
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._place_bits(self._data_bits(payload))
        self.mask = self._select_mask(forced_mask)

    # -- function patterns -------------------------------------------------
    @staticmethod
    def _format_coords(size: int) -> tuple[tuple[tuple[int, int], ...], ...]:
        """Coordinates of the two format-information copies, LSB first."""
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

    def _draw_finder(self, left: int, top: int) -> None:
        for dy in range(-1, 8):
            y = top + dy
            if y < 0 or y >= self.size:
                continue
            for dx in range(-1, 8):
                x = left + dx
                if x < 0 or x >= self.size:
                    continue
                dark = (
                    0 <= dx <= 6
                    and 0 <= dy <= 6
                    and (
                        dx in (0, 6)
                        or dy in (0, 6)
                        or (2 <= dx <= 4 and 2 <= dy <= 4)
                    )
                )
                self.modules[y][x] = dark
                self.function[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for left, top in ((0, 0), (size - 7, 0), (0, size - 7)):
            self._draw_finder(left, top)

        positions = _ALIGNMENT[self.version]
        for top in positions:
            for left in positions:
                if self.function[top][left]:
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        dark = max(abs(dx), abs(dy)) != 1
                        self.modules[top + dy][left + dx] = dark
                        self.function[top + dy][left + dx] = True

        for i in range(8, size - 8):
            if not self.function[i][6]:
                self.modules[i][6] = i % 2 == 0
                self.function[i][6] = True
            if not self.function[6][i]:
                self.modules[6][i] = i % 2 == 0
                self.function[6][i] = True

        first, second = self._format_coords(size)
        for row, col in first + second:
            self.function[row][col] = True
        self.function[size - 8][8] = True
        self.modules[size - 8][8] = True

        if self.version >= 7:
            bits = _version_bits(self.version)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                for row, col in (
                    (i // 3, size - 11 + i % 3),
                    (size - 11 + i % 3, i // 3),
                ):
                    self.modules[row][col] = dark
                    self.function[row][col] = True

    # -- data, error correction, interleaving ------------------------------
    def _data_bits(self, payload: bytes) -> list[int]:
        blocks = [
            (total, data)
            for count, total, data in _RS_BLOCKS_M[self.version]
            for _ in range(count)
        ]
        data_codewords = sum(data for _total, data in blocks)
        bit_limit = data_codewords * 8
        bits: list[int] = []

        def put(value: int, width: int) -> None:
            for shift in range(width - 1, -1, -1):
                bits.append((value >> shift) & 1)

        put(0b0100, 4)  # byte mode
        put(len(payload), 8 if self.version < 10 else 16)
        for byte in payload:
            put(byte, 8)

        assert len(bits) <= bit_limit
        bits.extend([0] * min(4, bit_limit - len(bits)))
        while len(bits) % 8:
            bits.append(0)

        data_words = bytearray()
        for offset in range(0, len(bits), 8):
            value = 0
            for bit in bits[offset : offset + 8]:
                value = (value << 1) | bit
            data_words.append(value)
        pad = (0xEC, 0x11)
        pad_index = 0
        while len(data_words) < data_codewords:
            data_words.append(pad[pad_index % 2])
            pad_index += 1
        assert len(data_words) == data_codewords

        data_blocks: list[list[int]] = []
        ecc_blocks: list[list[int]] = []
        offset = 0
        for total, data_count in blocks:
            block = list(data_words[offset : offset + data_count])
            offset += data_count
            data_blocks.append(block)
            ecc_blocks.append(_rs_ecc(block, total - data_count))
        assert offset == len(data_words)

        codewords = bytearray()
        for i in range(max(len(block) for block in data_blocks)):
            for block in data_blocks:
                if i < len(block):
                    codewords.append(block[i])
        for i in range(max(len(block) for block in ecc_blocks)):
            for block in ecc_blocks:
                if i < len(block):
                    codewords.append(block[i])
        assert len(codewords) == sum(total for total, _data in blocks)

        result: list[int] = []
        for word in codewords:
            for shift in range(7, -1, -1):
                result.append((word >> shift) & 1)
        result.extend([0] * _REMAINDER_BITS[self.version])
        return result

    def _place_bits(self, bits: list[int]) -> None:
        size = self.size
        index = 0
        row = size - 1
        direction = -1
        for col in range(size - 1, 0, -2):
            if col <= 6:
                col -= 1
            while True:
                for current_col in (col, col - 1):
                    if not self.function[row][current_col]:
                        self.modules[row][current_col] = (
                            bits[index] == 1 if index < len(bits) else False
                        )
                        index += 1
                row += direction
                if row < 0 or row >= size:
                    row -= direction
                    direction = -direction
                    break
        if index != len(bits):
            raise RuntimeError(
                f"QR data placement mismatch: {index} cells for {len(bits)} bits"
            )

    # -- masks and format information -------------------------------------
    def _put_format(self, matrix: list[list[bool]], mask: int) -> None:
        bits = _format_bits(mask)
        first, second = self._format_coords(self.size)
        for coords in (first, second):
            for i, (row, col) in enumerate(coords):
                matrix[row][col] = (bits >> i) & 1 == 1
        matrix[self.size - 8][8] = True

    @staticmethod
    def _penalty(matrix: list[list[bool]]) -> int:
        size = len(matrix)
        score = 0

        for row in matrix:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2

        for col in range(size):
            run = 1
            for row in range(1, size):
                if matrix[row][col] == matrix[row - 1][col]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2

        for row in range(size - 1):
            for col in range(size - 1):
                color = matrix[row][col]
                if (
                    matrix[row + 1][col] == color
                    and matrix[row][col + 1] == color
                    and matrix[row + 1][col + 1] == color
                ):
                    score += 3

        pattern_a = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)
        pattern_b = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)
        lines = [[int(cell) for cell in row] for row in matrix]
        lines.extend(
            [[int(matrix[row][col]) for row in range(size)] for col in range(size)]
        )
        for line in lines:
            for i in range(size - 10):
                window = tuple(line[i : i + 11])
                if window == pattern_a:
                    score += 40
                if window == pattern_b:
                    score += 40

        total = size * size
        dark = sum(sum(1 for cell in row if cell) for row in matrix)
        score += 10 * (abs(dark * 20 - total * 10) // total)
        return score

    def _select_mask(self, forced_mask: int | None) -> int:
        choices = range(8) if forced_mask is None else (forced_mask,)
        best_matrix = None
        best_mask = 0
        best_score = None
        for mask in choices:
            candidate = [row[:] for row in self.modules]
            for row in range(self.size):
                for col in range(self.size):
                    if not self.function[row][col] and _MASKS[mask](row, col):
                        candidate[row][col] = not candidate[row][col]
            self._put_format(candidate, mask)
            penalty = self._penalty(candidate)
            if best_score is None or penalty < best_score:
                best_matrix = candidate
                best_mask = mask
                best_score = penalty
        assert best_matrix is not None
        self.modules = best_matrix
        return best_mask


def _encode(text: str, version: int | None = None, mask: int | None = None) -> _Symbol:
    """Internal encoder used by the public renderers and the test suite."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text:
        raise ValueError("text must not be empty")
    payload = text.encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} UTF-8 bytes; maximum supported is "
            f"{MAX_PAYLOAD_BYTES} bytes at version 15-M"
        )
    if version is None:
        version = _pick_version(len(payload))
    elif not isinstance(version, int) or isinstance(version, bool):
        raise TypeError("version must be an int or None")
    elif version not in _RS_BLOCKS_M:
        raise ValueError(f"version must be between 1 and {max(_RS_BLOCKS_M)}")
    if not 4 + (8 if version < 10 else 16) + 8 * len(payload) <= _capacity_bits(
        version
    ):
        raise ValueError(f"payload does not fit in version {version}-M")
    if mask is not None and (
        not isinstance(mask, int) or isinstance(mask, bool) or not 0 <= mask <= 7
    ):
        raise ValueError("mask must be an integer between 0 and 7, or None")
    return _Symbol(payload, version, mask)


def _validate_args(scale: int | float, border: int) -> None:
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or (isinstance(scale, float) and not math.isfinite(scale))
        or scale <= 0
    ):
        raise ValueError("scale must be a positive finite number")
    if isinstance(border, bool) or not isinstance(border, int) or border < 0:
        raise ValueError("border must be a non-negative integer")


def _number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return f"{value:g}"


def qr_svg(text: str, scale: int | float = 6, border: int = 4) -> str:
    """Return a standalone SVG rendering of *text* as a QR Code.

    *scale* is pixels per module (positive int or float); *border* is the quiet
    zone in modules (non-negative int).  The same bytes are always encoded, so
    output is deterministic for a given text, scale, and border.
    """
    _validate_args(scale, border)
    symbol = _encode(text)
    span = symbol.size + 2 * border
    pixels = span * scale

    path_parts: list[str] = []
    for row, cells in enumerate(symbol.modules):
        col = 0
        while col < symbol.size:
            if not cells[col]:
                col += 1
                continue
            start = col
            while col < symbol.size and cells[col]:
                col += 1
            path_parts.append(
                f"M{start + border} {row + border}h{col - start}v1h-{col - start}z"
            )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_number(pixels)}" '
        f'height="{_number(pixels)}" viewBox="0 0 {span} {span}" role="img" '
        f'aria-label="QR code" data-qr-version="{symbol.version}" '
        f'data-qr-ecc="M" data-qr-mask="{symbol.mask}" '
        f'data-qr-modules="{symbol.size}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        f'<path fill="#000" d="{"".join(path_parts)}"/>'
        "</svg>\n"
    )


def qr_png(text: str, scale: int = 6, border: int = 4) -> bytes:
    """Return PNG bytes for *text*, rendered with Pillow.

    Raises RuntimeError when Pillow is not installed.  *scale* must be a
    positive integer for PNG because pixels cannot be subdivided.
    """
    _validate_args(scale, border)
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValueError("PNG scale must be a positive integer")
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - exercised only without Pillow
        raise RuntimeError("PNG output requires Pillow (PIL) to be installed") from exc

    symbol = _encode(text)
    span = symbol.size + 2 * border
    pixels = span * scale
    image = Image.new("L", (pixels, pixels), 255)
    draw = ImageDraw.Draw(image)
    for row, cells in enumerate(symbol.modules):
        col = 0
        while col < symbol.size:
            if not cells[col]:
                col += 1
                continue
            start = col
            while col < symbol.size and cells[col]:
                col += 1
            draw.rectangle(
                (
                    (start + border) * scale,
                    (row + border) * scale,
                    (col + border) * scale - 1,
                    (row + border + 1) * scale - 1,
                ),
                fill=0,
            )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _self_test() -> None:
    url = "https://example.com/boardwalk/share?src=qr_selftest"
    symbol = _encode(url)
    svg = qr_svg(url)
    if not (svg.startswith("<?xml") and "<svg " in svg and "<path " in svg):
        raise AssertionError("SVG output is structurally incomplete")
    print(f"URL bytes: {len(url.encode('utf-8'))}")
    print(f"QR version: {symbol.version}; ECC: M; mask: {symbol.mask}")
    print(f"Modules per side: {symbol.size} (plus 4-module border)")
    print(f"SVG bytes: {len(svg.encode('utf-8'))}")
    try:
        png = qr_png(url)
    except RuntimeError as exc:
        print(f"PNG: unavailable ({exc})")
    else:
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AssertionError("PNG output is not a PNG")
        print(f"PNG bytes: {len(png)}")
    print("SELF-TEST PASS")


if __name__ == "__main__":
    try:
        _self_test()
    except Exception as exc:  # selftest must fail loudly, without printing secrets
        print(f"SELF-TEST FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
