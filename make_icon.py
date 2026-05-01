"""Generate EVE Ore Scanner icon (pickaxe + ore crystal)."""
import struct
import zlib

# Colors
T = (0, 0, 0, 0)          # transparent
O1 = (240, 136, 62, 255)  # orange accent (app theme)
O2 = (200, 110, 45, 255)  # dark orange
O3 = (255, 168, 80, 255)  # highlight orange
G1 = (100, 85, 60, 255)   # handle dark
G2 = (140, 115, 75, 255)  # handle mid
G3 = (170, 145, 100, 255) # handle light
S1 = (80, 80, 90, 255)    # steel dark
S2 = (140, 145, 155, 255) # steel mid
S3 = (190, 195, 205, 255) # steel light
C1 = (220, 160, 50, 255)  # crystal amber
C2 = (255, 200, 80, 255)  # crystal highlight
C3 = (180, 120, 30, 255)  # crystal shadow
BG = (13, 17, 23, 255)    # app background for contrast

# 32x32 pixel art - pickaxe with ore crystal
# Design: pickaxe head top-right, handle diagonal, crystal bottom-left
ICON_32 = [
#  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 0
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 1
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,S2,S3,S3,S2, T, T, T, T, T, T],  # 2
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,S2,S3,S3,S3,S2,S1, T, T, T, T, T],  # 3
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,S1,S2,S3,S2,S2,S1, T, T, T, T, T, T],  # 4
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,S1,S2,S3,S2,S1, T, T, T, T, T, T, T, T],  # 5
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,O3,O1,S2,S2,S1, T, T, T, T, T, T, T, T, T],  # 6
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,O3,O1,O1,O2,S1, T, T, T, T, T, T, T, T, T, T],  # 7
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,O3,O1,O1,O2, T, T, T, T, T, T, T, T, T, T, T, T],  # 8
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T,O3,O1,O1,O2, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 9
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T,O3,O1,O1,O2, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 10
  [T, T, T, T, T, T, T, T, T, T, T, T, T,G3,O1,O1,O2, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 11
  [T, T, T, T, T, T, T, T, T, T, T, T,G3,G2,G3,O2, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 12
  [T, T, T, T, T, T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 13
  [T, T, T, T, T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 14
  [T, T, T, T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 15
  [T, T, T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 16
  [T, T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 17
  [T, T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 18
  [T, T, T, T, T,G3,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 19
  [T, T, T, T,C2,G2,G2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 20
  [T, T, T,C2,C1,C2,G1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 21
  [T, T,C2,C1,C1,C1,C2, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 22
  [T,C2,C1,C1,C3,C1,C1,C2, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 23
  [T,C1,C1,C3,C3,C3,C1,C1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 24
  [T,C1,C3,C3,C3,C3,C3,C1, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 25
  [T, T,C3,C3,C3,C3,C3, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 26
  [T, T, T,C3,C3,C3, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 27
  [T, T, T, T,C3, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 28
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 29
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 30
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],  # 31
]


def create_png(width, height, pixel_rows):
    """Create PNG bytes from pixel row data."""
    def chunk(ctype, data):
        c = ctype + data
        crc = struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack('>I', len(data)) + c + crc

    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)

    raw = b''
    for y in range(height):
        raw += b'\x00'  # filter: none
        for x in range(width):
            r, g, b, a = pixel_rows[y][x]
            raw += struct.pack('BBBB', r, g, b, a)

    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def scale_icon(rows, factor):
    """Scale pixel art up by integer factor."""
    scaled = []
    for row in rows:
        new_row = []
        for pixel in row:
            new_row.extend([pixel] * factor)
        for _ in range(factor):
            scaled.append(list(new_row))
    return scaled


def create_ico(png_list):
    """Create ICO from list of (width, height, png_bytes)."""
    header = struct.pack('<HHH', 0, 1, len(png_list))
    offset = 6 + 16 * len(png_list)
    directory = b''
    data = b''
    for w, h, png in png_list:
        iw = 0 if w >= 256 else w
        ih = 0 if h >= 256 else h
        directory += struct.pack('<BBBBHHII', iw, ih, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    return header + directory + data


# Generate multiple sizes
png_16 = create_png(16, 16, scale_icon(ICON_32, 1)[:16])  # crop to 16x16 won't work well

# For 16x16, create a simplified version
ICON_16 = [
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],
  [T, T, T, T, T, T, T, T, T, T, T,S2,S3,S2, T, T],
  [T, T, T, T, T, T, T, T, T, T,S1,S3,S1, T, T, T],
  [T, T, T, T, T, T, T, T, T,O3,O1,S1, T, T, T, T],
  [T, T, T, T, T, T, T, T,O3,O1,O2, T, T, T, T, T],
  [T, T, T, T, T, T, T,O3,O1,O2, T, T, T, T, T, T],
  [T, T, T, T, T, T,G3,G2,O2, T, T, T, T, T, T, T],
  [T, T, T, T, T,G3,G2,G1, T, T, T, T, T, T, T, T],
  [T, T, T, T,G3,G2,G1, T, T, T, T, T, T, T, T, T],
  [T, T, T,G3,G2,G1, T, T, T, T, T, T, T, T, T, T],
  [T, T,C2,C1,G1, T, T, T, T, T, T, T, T, T, T, T],
  [T,C2,C1,C1,C2, T, T, T, T, T, T, T, T, T, T, T],
  [C1,C1,C3,C1,C1, T, T, T, T, T, T, T, T, T, T, T],
  [T,C3,C3,C3, T, T, T, T, T, T, T, T, T, T, T, T],
  [T, T,C3, T, T, T, T, T, T, T, T, T, T, T, T, T],
  [T, T, T, T, T, T, T, T, T, T, T, T, T, T, T, T],
]

png16 = create_png(16, 16, ICON_16)
png32 = create_png(32, 32, ICON_32)

# 48x48: scale 16x16 by 3
scaled_48 = scale_icon(ICON_16, 3)
png48 = create_png(48, 48, scaled_48)

# 256x256: scale 32x32 by 8
scaled_256 = scale_icon(ICON_32, 8)
png256 = create_png(256, 256, scaled_256)

ico_data = create_ico([
    (16, 16, png16),
    (32, 32, png32),
    (48, 48, png48),
    (256, 256, png256),
])

with open('ore_scanner.ico', 'wb') as f:
    f.write(ico_data)

print(f"Created ore_scanner.ico ({len(ico_data):,} bytes)")
print("Sizes: 16x16, 32x32, 48x48, 256x256")
